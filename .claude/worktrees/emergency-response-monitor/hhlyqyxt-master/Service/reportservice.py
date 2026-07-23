import os
import time
from datetime import datetime

# todo 做成配置
report_path = r'/mnt/data/AgentProduction/outputs/docs_test'

def checkreportexistbydate(report_type , date):

    if report_type == 1:
        report_file_name = "haihe_keyarea_" + date + ".pdf"
    elif report_type == 2:
        report_file_name = "haihe_rainfall_forecast_7d_" + date + ".pdf"
    elif report_type == 3:
        report_file_name = "haihe_rainfall_forecast_" + date + ".pdf"

    if os.path.exists(os.path.join(report_path,report_file_name)):
        return True
    else:
        return False


def queryreportlistbydate(date):
    result = {
        "keyarea_report": {
            "filepath": None,
            "filename": "",
        },
        "forecast_7d_report": {
            "filepath": None,
            "filename": "",
        },
        "forecast_report": {
            "filepath": None,
            "filename": "",
        }
    }


    if checkreportexistbydate(1, date):
        file_path = os.path.join(report_path,"haihe_keyarea_" + date + ".pdf")
        # 获取文件的元数据
        file_stat = os.stat(file_path)

        # 获取文件的创建时间
        creation_time = file_stat.st_ctime
        print(datetime.fromtimestamp(creation_time))

        creation_time = datetime.fromtimestamp(creation_time)
        result["keyarea_report"]["filepath"] = "haihe_keyarea_" + date + ".pdf"
        result["keyarea_report"]["filename"] = date + "_海河流域关键区气象专报(更新于"+ creation_time.strftime("%Y-%m-%d %H点") + ")"

    if checkreportexistbydate(2, date):
        file_path = os.path.join(report_path,"haihe_rainfall_forecast_7d_" + date + ".pdf")
        # 获取文件的元数据
        file_stat = os.stat(file_path)

        # 获取文件的创建时间
        creation_time = file_stat.st_ctime
        creation_time = datetime.fromtimestamp(creation_time)
        result["forecast_7d_report"]["filepath"] = "haihe_rainfall_forecast_7d_" + date + ".pdf"
        result["forecast_7d_report"]["filename"] = date + "_海河流域天气快报(更新于"+ creation_time.strftime("%Y-%m-%d %H点") + ")"

    if checkreportexistbydate(3, date):
        file_path = os.path.join(report_path,"haihe_rainfall_forecast_" + date + ".pdf")
        # 获取文件的元数据
        file_stat = os.stat(file_path)

        # 获取文件的创建时间
        creation_time = file_stat.st_ctime
        creation_time = datetime.fromtimestamp(creation_time)
        result["forecast_report"]["filepath"] = "haihe_rainfall_forecast_" + date + ".pdf"
        result["forecast_report"]["filename"] = date + "_海河流域 10 天降水预报(更新于"+ creation_time.strftime("%Y-%m-%d %H点") + ")"

    return result

if __name__ == '__main__':
    res = queryreportlistbydate("2025-07-28")
    print(res)
