import json
import os
import signal
from datetime import datetime, timedelta

import geopandas
import numpy as np
import pandas
import pandas as pd
import requests
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_ERROR
from sqlalchemy import text

from Models.QyMinuteMonitor import QyMinuteMonitor
from ScheduledTask.emergency_response_monitor import run_emergency_response_monitor
from utils import create_rainstorm_impact_map
from utils.MusicTool import MusicClient, MusicConfig
from utils.db import Session, engine

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

tempfile = "./yangxiao.csv"

def readmindata(timestr):
    client = MusicClient(MusicConfig())

    # timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=31)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}]"
    res = client.get_surf_ele_in_basin_by_time("HHLY_JUECE", timestr,data_code="SURF_CHN_MUL_MIN",elements=("Station_levl,Lat,Lon,Alti,Admin_Code_CHN,Town_code,Station_Id_C,Datetime,IYMDHM,RYMDHM,UPDATE_TIME,City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,PRE"))

    df = pd.DataFrame(res)

    return df

def readmindatabytimerange(starttimestr,endtimestr):
    client = MusicClient(MusicConfig())

    # timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=31)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}]"
    res = client.get_surf_pre_in_basin_timerange("HHLY_JUECE", f"[{starttimestr},{endtimestr}]",data_code="SURF_CHN_MUL_MIN",elements=("Station_levl,Lat,Lon,Alti,Admin_Code_CHN,Town_code,Station_Id_C,Datetime,IYMDHM,RYMDHM,UPDATE_TIME,City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,PRE"))

    df = pd.DataFrame(res)

    return df


def unionmindatabytimerange(starttimestr,endtimestr):
    starttime = datetime.strptime(starttimestr, "%Y%m%d%H%M%S")
    endtime = datetime.strptime(endtimestr, "%Y%m%d%H%M%S")
    temptime = starttime + timedelta(minutes=1)
    df = None
    while temptime <= endtime:
        if df is None:
            df = readmindata(temptime.strftime("%Y%m%d%H%M%S"))
        else:
            df = pd.concat([df,readmindata(temptime.strftime("%Y%m%d%H%M%S"))])
        temptime = temptime + timedelta(minutes=1)

    # df.to_csv("hourdatabymin_2026063011_10.csv")
    return df

def unionmindataby10minuteto24h(endtimestr):

    endtime = datetime.strptime(endtimestr, "%Y%m%d%H%M%S") - timedelta(hours=8)
    starttime = endtime - timedelta(hours=24)

    if os.path.exists(tempfile):
        os.remove(tempfile)

    temptime = starttime + timedelta(hours=1)
    df = None
    while temptime <= endtime:
        tempstarttime = temptime - timedelta(hours=1) + timedelta(minutes=1)

        res = readmindatabytimerange(tempstarttime.strftime("%Y%m%d%H%M%S"),temptime.strftime("%Y%m%d%H%M%S"))

        if df is None:
            df = res
        else:
            df = pd.concat([df,res])

        temptime = temptime + timedelta(hours=1)

    # df = pandas.read_csv("testmin.csv")
    print(df.head())
    df["Datetime"] = pd.to_datetime(df["Datetime"], format="%Y-%m-%d %H:%M:%S") + pd.Timedelta(hours=8)
    df["PRE"] = df["PRE"].astype("float")
    df.loc[df["PRE"] > 99988,"PRE"] = 0
    df_5min = (
        df.set_index("Datetime")
        .groupby("Station_Id_C")
        .resample("5min", label="right", closed="right")
        .agg({
            # 降水字段：5分钟累计
            "PRE": "sum",

            # 站点基础信息：保留原始字段
            "Station_levl": "first",
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
        })
        .reset_index()
    )

    print(df_5min)
    # df.to_csv("testmin.csv")

    df_5min.to_csv(tempfile)


def calchourmaxpre(df_5min):
    # 确保时间字段是 datetime 类型
    df_5min["Datetime"] = pd.to_datetime(df_5min["Datetime"])

    # 确保降水是数值类型
    df_5min["PRE"] = pd.to_numeric(df_5min["PRE"], errors="coerce")

    # 按站点、时间排序
    df_5min = df_5min.sort_values(["Station_Id_C", "Datetime"])

    # 计算每个站点的滑动1小时降水量
    df_5min["PRE_1H_ROLL"] = (
        df_5min.groupby("Station_Id_C")["PRE"]
        .rolling(window=12, min_periods=12)
        .sum()
        .reset_index(level=0, drop=True)
    ).copy()

    # 每条记录对应的一小时结束时间
    df_5min["End_Time"] = df_5min["Datetime"]

    # 每条记录对应的一小时开始时间
    df_5min["Start_Time"] = df_5min["Datetime"] - pd.Timedelta(hours=1)
    max_value = df_5min["PRE_1H_ROLL"].max()

    max_rows = df_5min[df_5min["PRE_1H_ROLL"] == max_value]
    return max_rows


def calc24hourmaxpre(df_5min):
    df_5min = (
        df_5min.groupby("Station_Id_C", as_index=False, sort=False)
        .agg({
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
            "PRE": "sum",
        })
    ).copy()
    max_value = df_5min["PRE"].max()

    max_rows = df_5min[df_5min["PRE"] == max_value]
    return max_rows


def countstationnumbylevel(df_5min):
    df_5min["PRE"] = pd.to_numeric(df_5min["PRE"], errors="coerce").fillna(0)

    # 1. 统计每个站点24小时累计降水量
    station_24h = (
        df_5min.groupby("Station_Id_C", as_index=False)["PRE"]
        .sum()
        .rename(columns={"PRE": "PRE_24H"})
    )
    bins = [0.1, 10, 25, 50, 100, 250, np.inf]

    labels = [
        "0.1-10",
        "10-25",
        "25-50",
        "50-100",
        "100-250",
        "250以上"
    ]

    # 3. 分级
    # right=False 表示左闭右开：[0.1,10), [10,25), [25,50) ...
    station_24h["rain_level"] = pd.cut(
        station_24h["PRE_24H"],
        bins=bins,
        labels=labels,
        right=False
    )

    # 4. 统计每个等级的站点数量
    rain_count = (
        station_24h.groupby("rain_level", observed=False)["Station_Id_C"]
        .nunique()
        .reset_index(name="station_count")
    )

    return rain_count


def calcmaxdataseg5min():

    df_5min = pd.read_csv(tempfile)

    df_5min["Lon"] = df_5min["Lon"].astype("float")
    df_5min["Lat"] = df_5min["Lat"].astype("float")
    df_5min["PRE"] = df_5min["PRE"].astype("float")
    tj_df_5min = df_5min[df_5min["City"] == '天津市']

    # 计算最大小时降水量
    hourmaxpre = calchourmaxpre(df_5min).to_dict('records')
    tj_hourmaxpre = calchourmaxpre(tj_df_5min).to_dict('records')

    # 计算最大降水量
    hourmaxpre24 = calc24hourmaxpre(df_5min).to_dict('records')
    tj_hourmaxpre24 = calc24hourmaxpre(tj_df_5min).to_dict('records')

    stationnum = countstationnumbylevel(df_5min)
    tj_stationnum = countstationnumbylevel(tj_df_5min)

    # 计算天津暴雨应急等级
    tj_rainlevel = calctianjinrainlevel(tj_df_5min)
    # 平均降雨量
    avgpre =  float(calcavgrain(df_5min))
    tj_avgpre =  float(calcavgrain(tj_df_5min))

    # 计算面雨量
    riverrain = calcrivermaxrain(df_5min)


    end_time = df_5min["Datetime"].max()
    end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")
    station_count = df_5min["Station_Id_C"].nunique()

    # 计算水库潮汛信息
    shuiku = getreservoir((end_time-pd.Timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),end_time.strftime("%Y-%m-%d %H:%M:%S"))



    rain_count_dict = dict(
        zip(stationnum["rain_level"].astype(str), stationnum["station_count"])
    )

    count_01_10 = rain_count_dict.get("0.1-10", 0)
    count_10_25 = rain_count_dict.get("10-25", 0)
    count_25_50 = rain_count_dict.get("25-50", 0)
    count_50_100 = rain_count_dict.get("50-100", 0)
    count_100_250 = rain_count_dict.get("100-250", 0)
    count_250_up = rain_count_dict.get("250以上", 0)
    geojsonurl = ""
    impact_city=""
    if (count_50_100>0 or count_100_250>0 or count_250_up>0):
        # 计算河流影响

        result = create_rainstorm_impact_map(
            csv_path=tempfile,
            graph_path="/home/ev/haiheliuyubaoyuagent/yx-test/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test-data/river_directed_v6.pkl",
            output_dir="/root/zm_code/rainstorm_impact_output",
            public_base_url="http://10.226.107.130:7000/rainstorm_impact_output",
        )
        geojsonurl=result["geojson_url"]
        print(geojsonurl)

        river_gdf = geopandas.read_file(geojsonurl)

        zhijie_river = river_gdf[river_gdf["impact_type"] == "direct_buffer"]
        jianjie_river = river_gdf[river_gdf["impact_type"] == "downstream_50km"]

        polygon_gdf = geopandas.read_file(r"Service/shi.geojson")

        join_gdf = geopandas.sjoin(
            zhijie_river,
            polygon_gdf,
            how="inner",
            predicate="intersects"
        )

        impact_city = "、".join(list(set(join_gdf['name'].tolist())))

    tj_rain_count_dict = dict(
        zip(tj_stationnum["rain_level"].astype(str), tj_stationnum["station_count"])
    )

    tj_count_01_10 = tj_rain_count_dict.get("0.1-10", 0)
    tj_count_10_25 = tj_rain_count_dict.get("10-25", 0)
    tj_count_25_50 = tj_rain_count_dict.get("25-50", 0)
    tj_count_50_100 = tj_rain_count_dict.get("50-100", 0)
    tj_count_100_250 = tj_rain_count_dict.get("100-250", 0)
    tj_count_250_up = tj_rain_count_dict.get("250以上", 0)
    qmm = QyMinuteMonitor(datatime=end_time_str,
                        station_num=station_count,
                          rain_level_1=count_01_10,
                          rain_level_2=count_10_25,
                          rain_level_3=count_25_50,
                          rain_level_4=count_50_100,
                          rain_level_5=count_100_250,
                          rain_level_6=count_250_up,
                          tj_rain_level_1=tj_count_01_10,
                          tj_rain_level_2=tj_count_10_25,
                          tj_rain_level_3=tj_count_25_50,
                          tj_rain_level_4=tj_count_50_100,
                          tj_rain_level_5=tj_count_100_250,
                          tj_rain_level_6=tj_count_250_up,
                          mean_rain = avgpre,
                          tj_mean_rain=tj_avgpre,
                          geojsonurl = geojsonurl,
                          impact_city = impact_city)

    session = Session()

    session.add(qmm)

    session.commit()
    # 监测24小时最大降水站点数据入库

    if (len(hourmaxpre24) > 0) and(hourmaxpre24[0]['PRE'] > 0.1):
        for row in hourmaxpre24:

            sql = text(
                'INSERT INTO qy_minute_24h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_24h,minute_monitor_id,type) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_24h,:hour_monitor_id,0)'
            )

            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id= row["Station_Id_C"],
                province= row["Province"],
                city = row["City"],
                cnty= row["Cnty"],
                station_name = row["Station_Name"],
                pre_24h = row["PRE"],
                hour_monitor_id = qmm.id
            )

            session.execute(sql,param)

    # 天津24小时最大降水站点数据入库
    if (len(tj_hourmaxpre24) > 0) and(tj_hourmaxpre24[0]['PRE'] > 0.1):
        for row in tj_hourmaxpre24:

            sql = text(
                'INSERT INTO qy_minute_24h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_24h,minute_monitor_id,type) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_24h,:hour_monitor_id,1)'
            )

            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id= row["Station_Id_C"],
                province= row["Province"],
                city = row["City"],
                cnty= row["Cnty"],
                station_name = row["Station_Name"],
                pre_24h = row["PRE"],
                hour_monitor_id = qmm.id
            )

            session.execute(sql,param)

    # 海河流域一小时最大降水量入库

    if (len(hourmaxpre) > 0) and hourmaxpre[0]['PRE_1H_ROLL'] >0.1:
        for row in hourmaxpre:
            sql = text(
                'INSERT INTO qy_minute_1h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_1h,minute_monitor_id,starttime,endtime,type) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_1h,:hour_monitor_id,:starttime,:endtime,0)'
            )
            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id=row["Station_Id_C"],
                province=row["Province"],
                city=row["City"],
                cnty=row["Cnty"],
                station_name=row["Station_Name"],
                pre_1h=row["PRE_1H_ROLL"],
                hour_monitor_id=qmm.id,
                starttime = row["Start_Time"].strftime("%Y-%m-%d %H:%M:%S"),
                endtime = row["End_Time"].strftime("%Y-%m-%d %H:%M:%S")
            )
            session.execute(sql, param)
    # 天津区域1小时最大降水量站点入库
    if (len(tj_hourmaxpre) > 0) and tj_hourmaxpre[0]['PRE_1H_ROLL'] >0.1:
        for row in tj_hourmaxpre:
            sql = text(
                'INSERT INTO qy_minute_1h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_1h,minute_monitor_id,starttime,endtime,type) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_1h,:hour_monitor_id,:starttime,:endtime,1)'
            )
            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id=row["Station_Id_C"],
                province=row["Province"],
                city=row["City"],
                cnty=row["Cnty"],
                station_name=row["Station_Name"],
                pre_1h=row["PRE_1H_ROLL"],
                hour_monitor_id=qmm.id,
                starttime = row["Start_Time"].strftime("%Y-%m-%d %H:%M:%S"),
                endtime = row["End_Time"].strftime("%Y-%m-%d %H:%M:%S")
            )
            session.execute(sql, param)
    # 天津暴雨等级结果入库
    for row in tj_rainlevel:
        if row["rain_level"] >0 :
            sql = text(
                # 'INSERT INTO qy_minute_tj_rain_cnty (lon, lat, time_range, province, city,cnty,admin_code,station_name,station_id,sum_pre,rain_level,hour_monitor_id) VALUES(:lon, :lat, :time_range, :province, :city,:cnty,:admin_code,:station_name,:station_id,:sum_pre,:rain_level,:hour_monitor_id)'
                'INSERT INTO qy_minute_tj_rain_cnty (lon, lat, time_range, province, city,cnty,station_name,station_id,sum_pre,rain_level,minute_monitor_id) VALUES(:lon, :lat, :time_range, :province, :city,:cnty,:station_name,:station_id,:sum_pre,:rain_level,:minute_monitor_id)'
            )

            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                time_range=row["time_range"],
                province=row["Province"],
                city=row["City"],
                cnty=row["Cnty"],
                # admin_code=row["Admin_Code_CHN"],
                station_id=row["Station_Id_C"],
                station_name=row["Station_Name"],
                sum_pre=row["SUM_PRE_1h"],
                rain_level=row["rain_level"],
                minute_monitor_id=qmm.id
            )
            session.execute(sql, param)
    # 河系降水量入库
    if(len(riverrain) > 0):
        for row in riverrain:
            if row["AVG_PRE_24H"] == 0 :
                break
            sql = text("insert into qy_minute_zone_pre (zonename, pre,  minute_monitor_id) values (:zonename, :pre, :minute_monitor_id);")
            param = dict(zonename=row["zone_name"],pre=row["AVG_PRE_24H"],minute_monitor_id=qmm.id)
            session.execute(sql, param)
    # 水库信息入库
    if(len(shuiku) > 0):
        for row in shuiku:
            sql = text("insert into qy_shuike (lon, lat, overlimit, datetime, stationname, source, stationid, minute_monitor_id) values (:lon, :lat, :overlimit, :datetime, :stationname, :source, :stationid, :minute_monitor_id)")
            param = dict(lon=row["lan"], lat=row["lat"],overlimit=row["overLimit"],datetime=row["bjDatetime"].strftime("%Y-%m-%d %H:%M:%S"),stationname=row["stationName"],source=row["source"],stationid=row["stationId"], minute_monitor_id=qmm.id)
            session.execute(sql, param)
    session.commit()
    minute_monitor_id = qmm.id
    session.close()

    # 应急响应监测入库：独立拉取 HHLY 流域分钟降水（不再读共享 yangxiao.csv / HHLY_JUECE）。
    # timeRange 用 UTC（HHLY 接口口径），_fetch 内部把返回 Datetime +8h 转北京时间，
    # 与下方 datatime=end_time（BJT）的 12h/24h 窗口对齐。
    _emergency_timerange = (
        f"[{(end_time - timedelta(hours=32)).strftime('%Y%m%d%H%M%S')},"
        f"{(end_time - timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}]"
    )
    run_emergency_response_monitor(
        timerange=_emergency_timerange,
        datatime=end_time,
        minute_monitor_id=minute_monitor_id,
    )


def circleadd5min():
    df_5min = pd.read_csv(tempfile)
    if "Station_levl" not in df_5min.columns:
        df_5min["Station_levl"] = ""
    df_5min = df_5min[["Station_Id_C","Datetime","PRE","Station_levl","Lat","Lon","City","Station_Name","Cnty","Province","Town"]]
    df_5min["Datetime"] = pd.to_datetime(df_5min["Datetime"], format="%Y-%m-%d %H:%M:%S")
    end_time = df_5min["Datetime"].max()+pd.Timedelta(minutes=5)
    end_time_str = end_time.strftime("%Y-%m-%d %H:%M:%S")

    start_time = end_time - pd.Timedelta(hours=24)

    df = df_5min[
        (df_5min["Datetime"] > start_time) &
        (df_5min["Datetime"] <= end_time)
        ].copy()

    res = readmindatabytimerange((end_time-pd.Timedelta(minutes=4)-pd.Timedelta(hours=8)).strftime("%Y%m%d%H%M%S"), (end_time-pd.Timedelta(hours=8)).strftime("%Y%m%d%H%M%S"))


    res["Datetime"] = pd.to_datetime(res["Datetime"], format="%Y-%m-%d %H:%M:%S") + pd.Timedelta(hours=8)
    res["PRE"] = res["PRE"].astype("float")
    res.loc[res["PRE"] > 99988, "PRE"] = 0
    res = (
        res.set_index("Datetime")
        .groupby("Station_Id_C")
        .resample("5min", label="right", closed="right")
        .agg({
            # 降水字段：5分钟累计
            "PRE": "sum",

            # 站点基础信息：保留原始字段
            "Station_levl": "first",
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
        })
        .reset_index()
    )

    df_new = pd.concat([df, res], ignore_index=True)

    df_new= df_new.sort_values(by=['Station_Id_C', 'Datetime'], ascending=[True, False])

    df_new.to_csv(tempfile, index=False)

    return end_time.to_pydatetime()

def getreservoir(starttimestr,endtimestr):
    url = "http://10.226.107.35:8001/openapi/water_level/reservoir"
    headers = {
        "Content-Type": "application/json;charset=UTF-8"
    }
    body = {
        "areaIds": [
            6,
            7,
            8,
            9,
            10,
            11,
            12,
            13,
            14
        ],
        "beginTime": starttimestr,
        "endTime": endtimestr,
        "sources": [
            "hwdb"
        ]
    }

    response = requests.post(url, headers=headers, data=json.dumps(body))

    df = pd.DataFrame(response.json())


    # 转换时间字段
    df["bjDatetime"] = pd.to_datetime(df["bjDatetime"])
    df['overLimit'] = df['overLimit'].astype("float")
    df['lan'] = df['lan'].astype("float")
    df['lat'] = df['lat'].astype("float")
    latest_df = df.loc[df.groupby("stationId")["bjDatetime"].idxmax()]
    latest_df = latest_df[latest_df['overLimit']>0]
    print(latest_df)
    return latest_df.to_dict('records')


def calctianjinrainlevel(df_5min):
    end_time = df_5min["Datetime"].max()
    start_time = end_time - pd.Timedelta(hours=1)
    df = df_5min[
        (df_5min["Datetime"] > start_time) &
        (df_5min["Datetime"] <= end_time)
    ].copy()

    df = (
        df.groupby("Station_Id_C", as_index=False)
        .agg({
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
            "PRE": "sum",
        })
        .rename(columns={"PRE": "SUM_PRE_1h"})
    )

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour1result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"), 30, 50, 70, 100, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0, 1, 2, 3, 4]
    hour1result["rain_level"] = pd.cut(
        hour1result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )


    start_time = end_time - pd.Timedelta(hours=6)
    df = df_5min[
        (df_5min["Datetime"] > start_time) &
        (df_5min["Datetime"] <= end_time)
        ].copy()

    df = (
        df.groupby("Station_Id_C", as_index=False)
        .agg({
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
            "PRE": "sum",
        })
        .rename(columns={"PRE": "SUM_PRE_1h"})
    )

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour6result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"), 50, 70, 100, 150, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0, 1, 2, 3, 4]

    hour6result["rain_level"] = pd.cut(
        hour6result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )

    start_time = end_time - pd.Timedelta(hours=24)
    df = df_5min[
        (df_5min["Datetime"] > start_time) &
        (df_5min["Datetime"] <= end_time)
        ].copy()

    df = (
        df.groupby("Station_Id_C", as_index=False)
        .agg({
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
            "PRE": "sum",
        })
        .rename(columns={"PRE": "SUM_PRE_1h"})
    )

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour24result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"), 70, 100, 150, 200, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0, 1, 2, 3, 4]
    hour24result["rain_level"] = pd.cut(
        hour24result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )

    # print('hour1result', hour1result)
    # print('hour6result', hour6result)
    # print("hour24result", hour24result)

    hour1 = hour1result.copy()
    hour6 = hour6result.copy()
    hour24 = hour24result.copy()

    hour1["time_range"] = 1
    hour6["time_range"] = 6
    hour24["time_range"] = 24

    all_result = pd.concat([hour1, hour6, hour24], ignore_index=True)

    all_result["rain_level"] = pd.to_numeric(all_result["rain_level"], errors="coerce")
    all_result["SUM_PRE_1h"] = pd.to_numeric(all_result["SUM_PRE_1h"], errors="coerce")

    # 先按 Cnty 排序，再按 rain_level 和 SUM_PRE_1h 降序
    cnty_max_level = (
        all_result
        .sort_values(
            by=["Cnty", "rain_level", "time_range", "SUM_PRE_1h"],
            ascending=[True, False, True, False]
        )
        .drop_duplicates(subset="Cnty", keep="first")
        .reset_index(drop=True)
    )


    max_rows = cnty_max_level[cnty_max_level["rain_level"] == cnty_max_level["rain_level"].max()]

    max_rows = max_rows.to_dict('records')

    return max_rows

def calcavgrain(df_5min):
    station_24h = (
        df_5min.groupby("Station_Id_C", as_index=False)["PRE"]
        .sum()
        .rename(columns={"PRE": "PRE_24H"})
    )

    # 全市平均24小时降水量
    avg_24h_rain = station_24h["PRE_24H"].mean()

    return avg_24h_rain
def stationmonitorbyhour1(timestr):
    client = MusicClient(MusicConfig())

    timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S')-timedelta(hours=1)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S')).strftime('%Y%m%d%H%M%S')}]"
    res = client.stat_surf_pre_in_basin_new("HHLY_JUECE",timerange)

    df = pd.DataFrame(res)
    # df.to_csv("hourdata_2026063011_10.csv")

def calcrivermaxrain(df_5min):
    station_24h = (
        df_5min.groupby("Station_Id_C", as_index=False)
        .agg({
            "PRE": "sum",
            "Lon": "first",
            "Lat": "first",
            "Station_Name": "first",
            "City": "first",
            "Cnty": "first",
            "Province": "first"
        })
        .rename(columns={"PRE": "PRE_24H"})
    )

    station_gdf = geopandas.GeoDataFrame(
        station_24h,
        geometry=geopandas.points_from_xy(station_24h["Lon"], station_24h["Lat"]),
        crs="EPSG:4326"
    )

    polygon_gdf = geopandas.read_postgis(
        """
        SELECT gid ,
               zone_name,
               geom
        FROM public.haihe_zone_9
        """,
        engine,
        geom_col="geom"
    )

    join_gdf = geopandas.sjoin(
        station_gdf,
        polygon_gdf,
        how="inner",
        predicate="intersects"
    )

    polygon_avg_rain = (
        join_gdf.groupby(["gid", "zone_name"], as_index=False)
        .agg(
            AVG_PRE_24H=("PRE_24H", "mean"),
            MAX_PRE_24H=("PRE_24H", "max"),
            STATION_COUNT=("Station_Id_C", "nunique")
        )
    )

    print(polygon_avg_rain)
    max_rows = polygon_avg_rain[polygon_avg_rain["AVG_PRE_24H"] == polygon_avg_rain["AVG_PRE_24H"].max()]

    max_rows = max_rows.to_dict('records')

    return max_rows
def scheduler_listener(event):
    """监听任务跳过和执行异常。"""
    if event.code == EVENT_JOB_MAX_INSTANCES:
        print(
            "上一个任务尚未完成，本次五分钟任务跳过，原计划执行时间：%s",
            event.scheduled_run_times,
        )

    elif event.code == EVENT_JOB_ERROR:
        print(
            "任务执行异常：%s",
            event.exception,
        )

def process_task():
    endtime = datetime.now()

    datatime = circleadd5min()
    calcmaxdataseg5min()

    while endtime > datatime:
        datatime = circleadd5min()
        calcmaxdataseg5min()

def main():
    scheduler = BlockingScheduler(timezone="Asia/Shanghai")

    scheduler.add_job(
        func=process_task,
        trigger="cron",

        # 每个自然时钟的 00、05、10、15……分钟执行
        minute="*/5",
        second=0,

        id="five_minute_task",

        # 关键参数：同一任务最多只能运行一个实例
        max_instances=1,

        # 程序暂停或阻塞后出现多个过期任务时，只补执行一次
        coalesce=True,

        # 超过计划执行时间30秒，则视为过期
        misfire_grace_time=30,

        replace_existing=True,
    )

    scheduler.add_listener(
        scheduler_listener,
        EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR,
    )

    scheduler.start()

if __name__ == '__main__':
    # readmindata("20260703032400")
    # stationmonitorbyhour1("20260630110000")
    # unionmindatabytimerange("20260630100000","20260630110000")

    # unionmindataby10minuteto24h("20260703150000")

    # df = readmindatabytimerange("20260703100000","20260703150000")
    # print(df)

    # calcmaxdataseg5min()

    # circleadd5min()

    # unionmindataby10minuteto24h("20260711120000")
    # endtime = datetime.strptime("2026-07-11 22:00:00", "%Y-%m-%d %H:%M:%S")
    #
    # datatime = datetime.strptime("2025-07-27 13:55:00", "%Y-%m-%d %H:%M:%S")
    # datatime = circleadd5min()
    # calcmaxdataseg5min()
    #
    # while endtime > datatime:
    #     datatime = circleadd5min()
    #     calcmaxdataseg5min()

    # df_5min = pd.read_csv(tempfile)
    #
    # df_5min["Lon"] = df_5min["Lon"].astype("float")
    # df_5min["Lat"] = df_5min["Lat"].astype("float")
    # df_5min["PRE"] = df_5min["PRE"].astype("float")
    #
    # calcrivermaxrain(df_5min)

    # python -m ScheduledTask.stationProcessMin

    # result = create_rainstorm_impact_map(
    #     csv_path="/root/zm_code/new.csv",
    #     graph_path="/home/ev/haiheliuyubaoyuagent/yx-test/haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test-data/river_directed_v5.pkl",
    #     output_dir="/root/zm_code/rainstorm_impact_output",
    #     public_base_url = "http://10.226.107.130:7000/rainstorm_impact_output"
    # )
    #
    # map_json_url = result["map_package_url"]  # 专题图 JSON 地址
    # river_json_url = result["geojson_url"]  # 影响河流 GeoJSON 地址
    #
    # river_gdf = geopandas.read_file(river_json_url)
    #
    # zhijie_river = river_gdf[river_gdf["impact_type"] =="direct_buffer"]
    # jianjie_river = river_gdf[river_gdf["impact_type"] =="downstream_50km"]
    #
    # polygon_gdf = geopandas.read_file(r"Service/shi.geojson")
    #
    # join_gdf = geopandas.sjoin(
    #     zhijie_river,
    #     polygon_gdf,
    #     how="inner",
    #     predicate="intersects"
    # )
    #
    # impact_city = "、".join(list(set(join_gdf['name'].tolist())))
    #
    # print(map_json_url)
    # print(river_json_url)
    # print(river_gdf)
    # print("、".join(list(set(join_gdf['name'].tolist()))))

    # getreservoir()

    # calcmaxdataseg5min()

    # df_5min = pd.read_csv(r'C:\Users\123\Desktop\yangxiao.csv')
    #
    # df_5min["Lon"] = df_5min["Lon"].astype("float")
    # df_5min["Lat"] = df_5min["Lat"].astype("float")
    # df_5min["PRE"] = df_5min["PRE"].astype("float")
    # df_5min["Datetime"] = pd.to_datetime(df_5min["Datetime"])
    # tj_df_5min = df_5min[df_5min["City"] == '天津市']
    #
    # res= calctianjinrainlevel(df_5min)
    # print(res)


    # nohup python -u -m  ScheduledTask.stationProcessMin >stationProcessMin.log 2>&1 &

    main()


