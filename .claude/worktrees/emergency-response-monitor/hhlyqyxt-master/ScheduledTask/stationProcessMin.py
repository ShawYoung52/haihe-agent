import os
from datetime import datetime, timedelta

import pandas
import pandas as pd

from utils.MusicTool import MusicClient, MusicConfig
from ScheduledTask.emergency_response_monitor import run_emergency_response_monitor

tempfile = "./24hourmindata.csv"

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

    # endtime = datetime.strptime(endtimestr, "%Y%m%d%H%M%S")
    # starttime = endtime - timedelta(hours=24)
    #
    # if os.path.exists(tempfile):
    #     os.remove(tempfile)
    #
    # temptime = starttime + timedelta(hours=1)
    # df = None
    # while temptime <= endtime:
    #     tempstarttime = temptime - timedelta(hours=1) + timedelta(minutes=1)
    #
    #     res = readmindatabytimerange(tempstarttime.strftime("%Y%m%d%H%M%S"),temptime.strftime("%Y%m%d%H%M%S"))
    #
    #     if df is None:
    #         df = res
    #     else:
    #         df = pd.concat([df,res])
    #
    #     temptime = temptime + timedelta(hours=1)

    df = pandas.read_csv("testmin.csv")
    print(df.head())
    df["Datetime"] = pd.to_datetime(df["Datetime"], format="%Y-%m-%d %H:%M:%S")
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
    max_row = df_5min.loc[df_5min["PRE_1H_ROLL"].idxmax()]
    return max_row


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
    max_row = df_5min.loc[df_5min["PRE"].idxmax()]
    return max_row

def calcmaxdataseg5min():

    df_5min = pd.read_csv(tempfile)
    tj_df_5min = df_5min[df_5min["City"] == '天津市']

    # 计算最大小时降水量
    hourmaxpre = calchourmaxpre(df_5min)
    tj_hourmaxpre = calchourmaxpre(tj_df_5min)

    # 计算最大降水量

    hourmaxpre24 = calc24hourmaxpre(df_5min)
    tj_hourmaxpre24 = calc24hourmaxpre(tj_df_5min)


    print(hourmaxpre)

    print(tj_hourmaxpre)

    print(hourmaxpre24)
    print(tj_hourmaxpre24)

    # 计算应急响应级别并持久化
    end_time = pd.to_datetime(df_5min["Datetime"]).max()
    run_emergency_response_monitor(
        csv_path=tempfile,
        datatime=end_time,
    )






def stationmonitorbyhour1(timestr):
    client = MusicClient(MusicConfig())

    timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S')-timedelta(hours=1)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S')).strftime('%Y%m%d%H%M%S')}]"
    res = client.stat_surf_pre_in_basin_new("HHLY_JUECE",timerange)

    df = pd.DataFrame(res)
    # df.to_csv("hourdata_2026063011_10.csv")

if __name__ == '__main__':
    # readmindata("20260703032400")
    # stationmonitorbyhour1("20260630110000")
    # unionmindatabytimerange("20260630100000","20260630110000")

    # unionmindataby10minuteto24h("20260703150000")

    # df = readmindatabytimerange("20260703100000","20260703150000")
    # print(df)

    calcmaxdataseg5min()