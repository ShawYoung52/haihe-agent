from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

# from Controller.wx_router import wxrouter
from Controller.tool_router import toolrouter
from Schemas.selectQyByParam import SelectQyByParam
from Service.reportservice import queryreportlistbydate
from utils.db import Session

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"], # 允许的 HTTP 方法
    allow_headers=["*"] # 允许的 HTTP 请求头
)

app.mount("/front", StaticFiles(directory="./front"), name="static")


# app.include_router(wxrouter)
app.include_router(toolrouter)

@app.get("/")
async def root():
    return {"message": "Hello World"}


@app.post("/selectqybytimerange")
async def selectqybytimerange(param:SelectQyByParam):
    starttime = param.starttime
    endtime = param.endtime

    sql = text("select * from qy_hour_monitor  where date_time >= :starttime and date_time <= :endtime order by date_time asc ")

    param = dict(starttime=starttime,endtime=endtime)

    session = Session()
    cursor = session.execute(sql, param)

    monitorlist = cursor.fetchall()

    result = []

    for monitor in monitorlist:
        hourmonitor = dict(
            id = monitor[0],
            datatime = monitor[1].strftime("%Y-%m-%d %H:%M:%S"),
            createtime = monitor[2].strftime("%Y-%m-%d %H:%M:%S"),
            rain_station_sum = monitor[3],
            station_sum = monitor[4],
            max_rain_24h = monitor[5],
            rain_position = monitor[6],
            onehourmax = [],
            hour24max = [],
            tj_rain_station = []

        )

        sql = text(f"select * from qy_1h_max_station  where hour_monitor_id = {hourmonitor['id']}")
        cursor = session.execute(sql)

        onehourmax = cursor.fetchall()

        for one in onehourmax:
            print(one)
            hourmonitor['onehourmax'].append({
                "id":one[0],
                "lon":one[1],
                "lat":one[2],
                "station_id":one[3],
                "province" :one[4],
                "city":one[5],
                "cnty":one[6],
                "station_name":one[7],
                "pre_1h":one[8],
                "max_time":one[9].strftime("%Y-%m-%d %H:%M:%S"),
                "create_time":one[10].strftime("%Y-%m-%d %H:%M:%S"),
                "del_flag":one[11],
                "hour_monitor_id":one[12],
            })

        sql = text(f"select * from qy_24h_max_station  where hour_monitor_id = {hourmonitor['id']}")
        cursor = session.execute(sql)

        daymax = cursor.fetchall()



        for one in daymax:
            hourmonitor['hour24max'].append({
                "id": one[0],
                "lon": one[1],
                "lat": one[2],
                "station_id": one[3],
                "province": one[4],
                "city": one[5],
                "cnty": one[6],
                "station_name": one[7],
                "pre_24h": one[8],
                "create_time": one[9].strftime("%Y-%m-%d %H:%M:%S"),
                "del_flag": one[10],
            })


        sql = text(f"select id,lon,lat,time_range,province,city,cnty,admin_code,station_name,station_id,sum_pre,rain_level,hour_monitor_id from qy_tj_rain_cnty  where hour_monitor_id = {hourmonitor['id']} order by time_range asc, sum_pre desc ")
        cursor = session.execute(sql)

        tj_rain_station = cursor.fetchall()
        # tj_rain_station = sorted(tj_rain_station, key=lambda x: (-x[3], x[10]))

        for item in tj_rain_station:
            hourmonitor["tj_rain_station"].append({
                "id":item[0],
                "lon":item[1],
                "lat":item[2],
                "time_range":item[3],
                "province":item[4],
                "city":item[5],
                "cnty":item[6],
                "admin_code":item[7],
                "station_name":item[8],
                "station_id":item[9],
                "sum_pre":item[10],
                "rain_level":item[11],
                "hour_monitor_id":item[12],
                "haha":11
            })

        result.append(hourmonitor)




    # 查询对应的报告
    reportdata = {}
    startdate = datetime.strptime(starttime, "%Y-%m-%d %H:%M:%S").replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(days=1)
    enddate = datetime.strptime(endtime, "%Y-%m-%d %H:%M:%S").replace(hour=0, minute=0, second=0, microsecond=0)
    tempdate = startdate

    while tempdate <= enddate:

        reportdata[tempdate.strftime("%Y-%m-%d")] = queryreportlistbydate(tempdate.strftime("%Y-%m-%d"))


        tempdate = tempdate + timedelta(days=1)


    

    return {"data": result,"reportdata":reportdata}


if __name__ == "__main__":

    uvicorn.run(
        "main:app",  # 模块名:应用实例名
        host="0.0.0.0",
        port=7000,
        reload=True  # 开发时启用自动重载
    )

    # route add 10.226.107.0 mask 255.255.255.0 10.226.252.1
    # route add 10.226.120.0 mask 255.255.255.0 10.226.252.1
    # route add 10.226.64.0 mask 255.255.255.0 10.226.252.1
    # route add 10.1.65.0 mask 255.255.255.0 10.226.252.1
    # route add 10.226.188.0 mask 255.255.255.0 10.226.252.1
    # mount -t cifs //172.18.18.6/public-data  /mnt/data  -o username=ww,password=ww,iocharset=utf8


    # su - postgres
    # cd /usr/bin/
    # pg_ctl -D /home/pgsql -l /home/pgsql/logfile restart -m fast


    # systemctl restart haihe-chainlit
    # systemctl restart haihe-backend
    # systemctl restart haihe-mcp
    # systemctl restart haihe-geoserver


#     nohup python  -u main.py >20260629.log 2>&1 &
