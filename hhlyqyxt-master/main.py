from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
import geopandas as gpd

# from Controller.wx_router import wxrouter
from Controller.tool_router import toolrouter
from Schemas.selectQyByParam import SelectQyByParam
from Service.reportservice import queryreportlistbydate
from utils.db import Session

# 修改为你的 shp 文件路径
SHP_PATH = Path(r"./Service/shi.geojson")

# shp 中面名称字段
NAME_FIELD = "name"


class PointInput(BaseModel):
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"], # 允许的 HTTP 方法
    allow_headers=["*"] # 允许的 HTTP 请求头
)

app.mount("/front", StaticFiles(directory="./front"), name="static")
app.mount("/rainstorm_impact_output", StaticFiles(directory="./rainstorm_impact_output"), name="static")


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


@app.post("/selectqybytimerangedataminute")
async def selectqybytimerangedataminute(param: SelectQyByParam):
    starttime = param.starttime
    endtime = param.endtime

    sql = text(
        '''
        WITH latest_time AS (
            SELECT MAX(datatime) AS max_datatime
            FROM qy_minute_monitor
            WHERE datatime >= :starttime
              AND datatime <  :endtime
        )
        select 
               id,datatime,station_num,rain_level_1,rain_level_2,rain_level_3,rain_level_4,rain_level_5,rain_level_6,tj_rain_level_1,tj_rain_level_2,tj_rain_level_3,tj_rain_level_4,tj_rain_level_5,tj_rain_level_6,mean_rain,tj_mean_rain,geojsonurl,impact_city
           from qy_minute_monitor  
           where 
               datatime >= :starttime 
             and datatime <= :endtime 
             AND (
                datatime = date_trunc('hour', datatime)
                OR datatime = (SELECT max_datatime FROM latest_time)
              )
           order by datatime asc ''')

    param = dict(starttime=starttime, endtime=endtime)

    session = Session()
    cursor = session.execute(sql, param)

    monitorlist = cursor.fetchall()

    result = []

    for monitor in monitorlist:
        hourmonitor = dict(
            id=monitor[0],
            datatime=monitor[1].strftime("%Y-%m-%d %H:%M:%S"),
            station_num=monitor[2],
            rain_level_1=monitor[3],
            rain_level_2=monitor[4],
            rain_level_3=monitor[5],
            rain_level_4=monitor[6],
            rain_level_5=monitor[7],
            rain_level_6=monitor[8],
            tj_rain_level_1=monitor[9],
            tj_rain_level_2=monitor[10],
            tj_rain_level_3=monitor[11],
            tj_rain_level_4=monitor[12],
            tj_rain_level_5=monitor[13],
            tj_rain_level_6=monitor[14],
            mean_rain=monitor[15],
            tj_mean_rain=monitor[16],
            geojsonurl=monitor[17],
            impact_city=monitor[18],
            onehourmax=[],
            tjonehourmax=[],
            hour24max=[],
            tjhour24max=[],
            tj_rain_station=[],
            zonepre = [],
            shuiku = []

        )

        sql = text(f"select id, lon, lat, station_id, province, city, cnty, station_name, pre_1h, starttime,endtime, minute_monitor_id,type from qy_minute_1h_max_station where minute_monitor_id = {hourmonitor['id']} and type = '0'")
        cursor = session.execute(sql)

        onehourmax = cursor.fetchall()

        for one in onehourmax:
            hourmonitor['onehourmax'].append({
                "id": one[0],
                "lon": one[1],
                "lat": one[2],
                "station_id": one[3],
                "province": one[4],
                "city": one[5],
                "cnty": one[6],
                "station_name": one[7],
                "pre_1h": one[8],
                "starttime": one[9].strftime("%Y-%m-%d %H:%M:%S"),
                "endtime": one[10].strftime("%Y-%m-%d %H:%M:%S"),
                "minute_monitor_id": one[11],
                "type": one[12],
            })

        sql = text(
            f"select id, lon, lat, station_id, province, city, cnty, station_name, pre_1h, starttime,endtime, minute_monitor_id,  type from qy_minute_1h_max_station where minute_monitor_id = {hourmonitor['id']} and type = '1'")
        cursor = session.execute(sql)

        onehourmax = cursor.fetchall()

        for one in onehourmax:
            hourmonitor['tjonehourmax'].append({
                "id": one[0],
                "lon": one[1],
                "lat": one[2],
                "station_id": one[3],
                "province": one[4],
                "city": one[5],
                "cnty": one[6],
                "station_name": one[7],
                "pre_1h": one[8],
                "starttime": one[9].strftime("%Y-%m-%d %H:%M:%S"),
                "endtime": one[10].strftime("%Y-%m-%d %H:%M:%S"),
                "minute_monitor_id": one[11],
                "type": one[12],
            })

        sql = text(f"select id, lon, lat, station_id, province, city, cnty, station_name, pre_24h,create_time, del_flag, minute_monitor_id, type from qy_minute_24h_max_station  where minute_monitor_id = {hourmonitor['id']} and type = '0'")
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
                "type": one[11],
            })

        sql = text(
            f"select id, lon, lat, station_id, province, city, cnty, station_name, pre_24h,create_time, del_flag, minute_monitor_id, type from qy_minute_24h_max_station  where minute_monitor_id = {hourmonitor['id']} and type = '1'")
        cursor = session.execute(sql)

        daymax = cursor.fetchall()

        for one in daymax:
            hourmonitor['tjhour24max'].append({
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
                "type": one[11],
            })

        sql = text(
            f"select id,lon,lat,time_range,province,city,cnty,admin_code,station_name,station_id,sum_pre,rain_level,minute_monitor_id from qy_minute_tj_rain_cnty  where minute_monitor_id = {hourmonitor['id']} order by time_range asc, sum_pre desc ")
        cursor = session.execute(sql)

        tj_rain_station = cursor.fetchall()
        # tj_rain_station = sorted(tj_rain_station, key=lambda x: (-x[3], x[10]))

        for item in tj_rain_station:
            hourmonitor["tj_rain_station"].append({
                "id": item[0],
                "lon": item[1],
                "lat": item[2],
                "time_range": item[3],
                "province": item[4],
                "city": item[5],
                "cnty": item[6],
                "admin_code": item[7],
                "station_name": item[8],
                "station_id": item[9],
                "sum_pre": item[10],
                "rain_level": item[11],
                "minute_monitor_id": item[12],
                "haha": 11
            })

        sql = text(
            f"select zonename,pre from qy_minute_zone_pre where minute_monitor_id = {hourmonitor['id']} ")
        cursor = session.execute(sql)

        zonepre = cursor.fetchall()

        for item in zonepre:
            hourmonitor["zonepre"].append({
                "zonename": item[0],
                "pre": item[1]
            })

        sql = text(
            f"select stationname from qy_shuike where minute_monitor_id = {hourmonitor['id']} ")
        cursor = session.execute(sql)

        shuiku = cursor.fetchall()

        for item in shuiku:
            hourmonitor["shuiku"].append({
                "stationname": item[0]
            })



        result.append(hourmonitor)
    session.close()

    # 查询对应的报告
    reportdata = {}
    startdate = datetime.strptime(starttime, "%Y-%m-%d %H:%M:%S").replace(hour=0, minute=0, second=0,
                                                                          microsecond=0) - timedelta(days=1)
    enddate = datetime.strptime(endtime, "%Y-%m-%d %H:%M:%S").replace(hour=0, minute=0, second=0, microsecond=0)
    tempdate = startdate

    while tempdate <= enddate:
        reportdata[tempdate.strftime("%Y-%m-%d")] = queryreportlistbydate(tempdate.strftime("%Y-%m-%d"))

        tempdate = tempdate + timedelta(days=1)

    return {"data": result, "reportdata": reportdata}


@app.post("/point-statistics")
def point_area_statistics(points: list[PointInput]):
    """
    统计每个面中包含的输入点数量，
    并按点数量从多到少返回面名称。
    """

    if not points:
        return {
            "input_count": 0,
            "matched_point_count": 0,
            "unmatched_point_count": 0,
            "data": []
        }

    polygon_gdf = gpd.read_file(SHP_PATH)

    if polygon_gdf is None or polygon_gdf.empty:
        raise HTTPException(
            status_code=500,
            detail="面数据未加载或为空"
        )

    # 构造点 GeoDataFrame
    point_gdf = gpd.GeoDataFrame(
        {
            "point_index": range(len(points)),
            "lon": [point.lon for point in points],
            "lat": [point.lat for point in points]
        },
        geometry=gpd.points_from_xy(
            [point.lon for point in points],
            [point.lat for point in points]
        ),
        crs="EPSG:4326"
    )

    # 空间连接
    # intersects 会包含位于面边界上的点
    joined = gpd.sjoin(
        point_gdf,
        polygon_gdf,
        how="left",
        predicate="intersects"
    )

    # 只保留匹配到面的点
    matched = joined.dropna(subset=[NAME_FIELD]).copy()

    if matched.empty:
        return {
            "input_count": len(points),
            "matched_point_count": 0,
            "unmatched_point_count": len(points),
            "data": []
        }

    # 按面名称统计点数量
    # nunique 防止同一个点因为异常重复匹配而被重复统计
    statistics = (
        matched
        .groupby(NAME_FIELD, as_index=False)
        .agg(point_count=("point_index", "nunique"))
        .sort_values(
            by=["point_count", NAME_FIELD],
            ascending=[False, True]
        )
    )

    # 匹配到任意面的点数量
    matched_point_count = int(
        matched["point_index"].nunique()
    )

    result = [
        {
            "name": row[NAME_FIELD],
            "point_count": int(row["point_count"])
        }
        for _, row in statistics.iterrows()
    ]

    return {
        "input_count": len(points),
        "matched_point_count": matched_point_count,
        "unmatched_point_count": len(points) - matched_point_count,
        "data": result
    }


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
