import signal
import time
from datetime import datetime, timedelta

import pandas as pd
from concurrent.futures import ProcessPoolExecutor

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from Models.QyHourMonitor import QyHourMonitor
from main import app
from utils.MusicTool import MusicConfig, MusicClient
from utils.db import engine, Session

process_pool = ProcessPoolExecutor(max_workers=2)
scheduler = BlockingScheduler()

running = True

def stationmonitorbyhour(timestr):
    client = MusicClient(MusicConfig())

    timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S')-timedelta(hours=31)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S')-timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}]"
    res = client.stat_surf_pre_in_basin_new("HHLY_JUECE",timerange)

    df = pd.DataFrame(res)

    # df.to_csv("./Day_" + (datetime.strptime(timestr, "%Y%m%d%H%M%S")-timedelta(hours=8)).strftime("%Y%m%d%H%M%S") + ".csv")

    df["Lon"] = df["Lon"].astype("float")
    df["Lat"] = df["Lat"].astype("float")
    df["SUM_PRE_1H"] = df["SUM_PRE_1H"].astype("float")
    df["COUNT_PRE_1H"] = df["COUNT_PRE_1H"].astype("int")

    df = df[df["SUM_PRE_1H"] <99999]

    df = (
        df.groupby("Station_Id_C", as_index=False, sort=False)
        .agg({
            "Lat": "first",
            "Lon": "first",
            "City": "first",
            "Station_Name": "first",
            "Cnty": "first",
            "Province": "first",
            "Town": "first",
            "SUM_PRE_1H": "sum",
            "COUNT_PRE_1H": "sum"  # 如果这个字段表示累计次数，建议也求和
        })
    )



    rainstationsum  = int((df["SUM_PRE_1H"] >= 50).sum())
    total = len(df)

    max_rows = df[df["SUM_PRE_1H"] == df["SUM_PRE_1H"].max()]
    max_rows = max_rows[max_rows["SUM_PRE_1H"] > 0.1]

    max_rows = max_rows.to_dict('records')

    max_rain = 0

    positionstr = ""
    if (len(max_rows) > 0):

        max_rain = max_rows[0]["SUM_PRE_1H"]
        # 经度 114 - 117 纬度 38 -40
        threshold = 0
        if max_rain >= 0.1 and max_rain < 10:

            threshold = 0.1
        elif max_rain >= 10 and max_rain < 25:
            threshold = 10
        elif max_rain >= 25 and max_rain < 50:
            threshold = 25
        elif max_rain >= 50 and max_rain < 100:
            threshold = 50
        elif max_rain >= 100 and max_rain < 250:
            threshold = 100
        elif max_rain >= 250:
            threshold = 250

        df = df[df["SUM_PRE_1H"] >= threshold]
        position = []


        if len(df[(df["Lon"] > 117) & (df["Lat"] >= 38) & (df["Lat"] <= 40)])>0:
            position.append("东部")
        if len(df[(df["Lon"] < 114) & (df["Lat"] >= 38) & (df["Lat"] <= 40)])>0:
            position.append("西部")
        if len(df[(df["Lon"] >= 114) & (df["Lon"] <= 117) &(df["Lat"] > 40)]) > 0:
            position.append("北部")
        if len(df[(df["Lon"] >= 114) & (df["Lon"] <= 117) &(df["Lat"] < 38)]) > 0:
            position.append("南部")
        if len(df[(df["Lon"] >= 114) & (df["Lon"] <= 117) & (df["Lat"] >= 38) & (df["Lat"] <= 40)]) > 0:
            position.append("中部")

        if len(df[(df["Lon"] > 117) &  (df["Lat"] > 40)]) > 0:
            position.append("东北部")
        if len(df[(df["Lon"] <114 ) &  (df["Lat"] > 40)]) > 0:
            position.append("西北部")

        if len(df[(df["Lon"] > 117) &  (df["Lat"] < 38)]) > 0:
            position.append("东南部")
        if len(df[(df["Lon"] < 114 ) &  (df["Lat"] < 38)]) > 0:
            position.append("西南部")

        positionstr = "、".join(position)

    qhm = QyHourMonitor(date_time = datetime.strptime(timestr, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M:%S"),rain_station_sum=rainstationsum,station_sum=total,max_rain_24h = max_rain,rain_position = positionstr)

    session = Session()

    session.add(qhm)

    session.commit()

    if (len(max_rows) > 0) and(max_rows[0]['SUM_PRE_1H'] > 0.1):
        for row in max_rows:

            sql = text(
                'INSERT INTO qy_24h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_24h,hour_monitor_id) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_24h,:hour_monitor_id)'
            )

            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id= row["Station_Id_C"],
                province= row["Province"],
                city = row["City"],
                cnty= row["Cnty"],
                station_name = row["Station_Name"],
                pre_24h = row["SUM_PRE_1H"],
                hour_monitor_id = qhm.id
            )

            session.execute(sql,param)


    df_24h = None
    for i in range(24):

        temptime = datetime.strptime(timestr, "%Y%m%d%H%M%S") - timedelta(hours=i+8)

        res = client.get_surf_ele_in_basin_by_time("HHLY_JUECE", temptime.strftime("%Y%m%d%H%M%S"))

        df = pd.DataFrame(res)

        # df.to_csv("./hour_" + temptime.strftime("%Y%m%d%H%M%S") + ".csv")

        df["PRE_1h"] = df["PRE_1h"].astype("float")

        df.loc[df["PRE_1h"] > 9999, "PRE_1h"] = 0

        max_rows = df[df["PRE_1h"] == df["PRE_1h"].max()]

        max_rows["datetime"] = (temptime+timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

        max_rows = max_rows.to_dict('records')

        if df_24h is None:
            df_24h = max_rows
        else:
            df_24h.extend(max_rows)


    df = pd.DataFrame(df_24h)

    max_rows = df[df["PRE_1h"] == df["PRE_1h"].max()]

    max_rows = max_rows.to_dict('records')


    if (len(max_rows) > 0) and max_rows[0]['PRE_1h'] >0.1:
        for row in max_rows:
            sql = text(
                'INSERT INTO qy_1h_max_station (lon, lat, station_id, province, city,cnty,station_name,pre_1h,max_time,hour_monitor_id) VALUES(:lon, :lat, :station_id, :province, :city,:cnty,:station_name,:pre_1h,:max_time,:hour_monitor_id)'
            )
            param = dict(
                lon=row["Lon"],
                lat=row["Lat"],
                station_id=row["Station_Id_C"],
                province=row["Province"],
                city=row["City"],
                cnty=row["Cnty"],
                station_name=row["Station_Name"],
                pre_1h=row["PRE_1h"],
                max_time=row["datetime"],
                hour_monitor_id=qhm.id
            )
            session.execute(sql, param)

    # 做天津的实况监测
    # 时间范围从1小时、6小时、24小时

    # 先进行1小时
    endtime = datetime.strptime(timestr, "%Y%m%d%H%M%S") - timedelta(hours=8)
    starttime = datetime.strptime(timestr, "%Y%m%d%H%M%S") - timedelta(hours=8)

    res = client.stat_surf_ele_in_region("120000", f"[{starttime.strftime('%Y%m%d%H%M%S')},{endtime.strftime('%Y%m%d%H%M%S')}]")

    df = pd.DataFrame(res)

    df["Lon"] = df["Lon"].astype("float")
    df["Lat"] = df["Lat"].astype("float")
    df["SUM_PRE_1h"] = df["SUM_PRE_1h"].astype("float")
    df = df[df["SUM_PRE_1h"] < 90000]

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour1result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"),30,50,70,100, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0,1,2,3,4]
    hour1result["rain_level"] = pd.cut(
        hour1result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )

    starttime = datetime.strptime(timestr, "%Y%m%d%H%M%S") - timedelta(hours=8+5)

    res = client.stat_surf_ele_in_region("120000",
                                         f"[{starttime.strftime('%Y%m%d%H%M%S')},{endtime.strftime('%Y%m%d%H%M%S')}]")

    df = pd.DataFrame(res)
    df["Lon"] = df["Lon"].astype("float")
    df["Lat"] = df["Lat"].astype("float")
    df["SUM_PRE_1h"] = df["SUM_PRE_1h"].astype("float")
    df = df[df["SUM_PRE_1h"] < 90000]

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour6result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"), 50,70,100,150, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0, 1, 2, 3, 4]

    hour6result["rain_level"] = pd.cut(
        hour6result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )

    starttime = datetime.strptime(timestr, "%Y%m%d%H%M%S") - timedelta(hours=8 + 23)

    res = client.stat_surf_ele_in_region("120000",
                                         f"[{starttime.strftime('%Y%m%d%H%M%S')},{endtime.strftime('%Y%m%d%H%M%S')}]")

    df = pd.DataFrame(res)
    df["Lon"] = df["Lon"].astype("float")
    df["Lat"] = df["Lat"].astype("float")
    df["SUM_PRE_1h"] = df["SUM_PRE_1h"].astype("float")
    df = df[df["SUM_PRE_1h"] < 90000]

    # 每个 Cnty 中 SUM_PRE_1h 最大值对应的行索引
    idx = df.groupby("Cnty")["SUM_PRE_1h"].idxmax()

    # 取出对应的整行数据
    hour24result = df.loc[idx].reset_index(drop=True)

    bins = [-float("inf"), 70,100,150,200, float("inf")]
    # 0:无暴雨预警；1：蓝色；2：黄色；3：橙色；4：红色
    labels = [0, 1, 2, 3, 4]
    hour24result["rain_level"] = pd.cut(
        hour24result["SUM_PRE_1h"],
        bins=bins,
        labels=labels,
        right=True
    )

    print('hour1result',hour1result)
    print('hour6result',hour6result)
    print("hour24result",hour24result)

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
            by=["Cnty", "rain_level","time_range", "SUM_PRE_1h"],
            ascending=[True, False,True, False]
        )
        .drop_duplicates(subset="Cnty", keep="first")
        .reset_index(drop=True)
    )
    show_cols = [
        "Cnty", "time_range", "Station_Id_C",
        "Lat", "Lon", "SUM_PRE_1h", "rain_level","Station_Name",
        "Town_code", "City", "Cnty", "Province","Admin_Code_CHN"
    ]

    print(cnty_max_level[show_cols])

    max_rows = cnty_max_level[cnty_max_level["rain_level"] == cnty_max_level["rain_level"].max()]

    max_rows = max_rows.to_dict('records')

    for row in max_rows:
        if row["rain_level"] >0 :
            sql = text(
                # 'INSERT INTO qy_tj_rain_cnty (lon, lat, time_range, province, city,cnty,admin_code,station_name,station_id,sum_pre,rain_level,hour_monitor_id) VALUES(:lon, :lat, :time_range, :province, :city,:cnty,:admin_code,:station_name,:station_id,:sum_pre,:rain_level,:hour_monitor_id)'
                'INSERT INTO qy_tj_rain_cnty (lon, lat, time_range, province, city,cnty,station_name,station_id,sum_pre,rain_level,hour_monitor_id) VALUES(:lon, :lat, :time_range, :province, :city,:cnty,:station_name,:station_id,:sum_pre,:rain_level,:hour_monitor_id)'
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
                hour_monitor_id=qhm.id
            )
            session.execute(sql, param)

    session.commit()

def submit_task():
    """
    APScheduler 每小时第 20 分调用这个函数。
    它只负责提交任务到进程池，不直接执行耗时逻辑。
    """
    now = datetime.now()

    # 当前时间整点字符串
    hour_str = now.replace(minute=0, second=0, microsecond=0).strftime(
        "%Y%m%d%H%M%S"
    )

    process_pool.submit(stationmonitorbyhour, hour_str)


def stop_handler(signum, frame):
    """
    接收 kill 或 Ctrl+C 时优雅退出。
    """
    global running
    print(f"收到停止信号: {signum}")
    running = False


def main():
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    scheduler.add_job(
        submit_task,
        CronTrigger(minute=30),
        id="hourly_task_at_20",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    print("scheduler started")

    try:
        while running:
            time.sleep(1)
    finally:
        print("scheduler stopping...")
        scheduler.shutdown(wait=False)
        process_pool.shutdown(wait=False)
        print("scheduler stopped")

if __name__ == '__main__':
    stationmonitorbyhour("20260608000000")
    stationmonitorbyhour("20260608010000")
    stationmonitorbyhour("20260608020000")
    stationmonitorbyhour("20260608030000")
    stationmonitorbyhour("20260608040000")
    stationmonitorbyhour("20260608050000")
    stationmonitorbyhour("20260608060000")
    stationmonitorbyhour("20260608070000")
    stationmonitorbyhour("20260608080000")
    stationmonitorbyhour("20260608090000")
    stationmonitorbyhour("20260608100000")
    # stationmonitorbyhour("20260608110000")
    # stationmonitorbyhour("20260608120000")
    # stationmonitorbyhour("20260608130000")
    # stationmonitorbyhour("20260608140000")
    # stationmonitorbyhour("20260608150000")
    # stationmonitorbyhour("20260608160000")
    # stationmonitorbyhour("20260608170000")
    # stationmonitorbyhour("20260608180000")
    # stationmonitorbyhour("20260608190000")
    # stationmonitorbyhour("20260608200000")
    # stationmonitorbyhour("20260608210000")
    # stationmonitorbyhour("20260608220000")
    # stationmonitorbyhour("20260608230000")

    # main()



    # stationmonitorbyhour("20260604000000")

    #nohup python -u -m  ScheduledTask.stationProcess >stationProcess.log 2>&1 &