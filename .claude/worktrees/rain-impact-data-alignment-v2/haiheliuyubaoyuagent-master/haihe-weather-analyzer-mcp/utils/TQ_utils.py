import hashlib
import json
import logging
import time
import uuid

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def getSign(signParams):
    '''
                生成sign标签
    '''
    sign = ""
    try:
        paramString = ""
        if ("params" in signParams):
            paramsVal = signParams.pop("params")
            keyValList = paramsVal.split("&")
            for keyVal in keyValList:
                signParams[keyVal.split("=")[0]] = keyVal.split("=")[1]

        # keys = signParams.keys()
        # keys.sort()
        keys = sorted(signParams)
        for key in keys:
            paramString = paramString + key + "=" + signParams.get(key) + "&"
        if (paramString):
            paramString = paramString[:-1]

        # 进行MD5运算
        sign = hashlib.md5(paramString.encode(encoding='UTF-8')).hexdigest().upper()
    except Exception as e:
        raise e
        # return("generate sign error")

    return sign


def send_get_request_requests(url):
    try:
        # 发送 GET 请求
        response = requests.get(url)
        body = response.content.decode('utf-8')
        return body
    except requests.exceptions.RequestException as e:
        print(f"请求失败: {e}")
        return str(e)


# 这个没问题
def get_pre_1h():
    order_by = 'Datetime:desc'

    time_range = '(20250723000000,20250724000000]'

    elements = 'Station_Id_C,Lon,Lat,PRE_6h,Datetime'

    data_code = 'SURF_CHN_MUL_HOR'

    interface_id = 'getSurfEleInRectByTimeRange'
    # 服务节点
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    # 接口服务端IP和端口
    serviceIp = '10.226.90.120'
    # 用户名&密码
    # userId = 'BETJ_QXT_JCYJ'
    # pwd = 'JIANCEyujing123!'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    # 序列化格式
    dataFormat = 'json'
    limitCnt = '1'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"timeRange={time_range}&"
        f"minLon=111.8&maxLon=119.847&maxLat=42.8&minLat=34.993&"
        f"orderby={order_by}&dataFormat={dataFormat}&limitCnt={limitCnt}"
    )

    # 生成时间戳和uuid，并拼接接口url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    # 生成sign
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'timeRange': time_range,
                  "minLon": '111.8',
                  "maxLon": '119.847',
                  "minLat": '34.993',
                  "maxLat": '42.8',
                  'orderby': order_by,
                  'dataFormat': dataFormat,
                  'limitCnt': limitCnt,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接sign
    baseUrl += '&sign=' + sign
    print(baseUrl)
    # 当前浏览器打开新标签
    # webbrowser.open_new_tab(baseUrl)
    res = send_get_request_requests(baseUrl)
    print(res)


def getSurfEleByTime():
    times = '20250722010000,20250722020000,20250722030000'

    elements = 'Station_Id_C,PRE_1h'
    # statEles = 'MAX_PRE_Time_0808'
    order_by = 'Station_Id_C'
    data_code = 'SURF_CHN_MUL_DAY'

    interface_id = 'getSurfEleByTime'
    # 服务节点
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    # 接口服务端IP和端口
    serviceIp = '10.226.90.120'
    # 用户名&密码
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    # userId = 'BETJ_QXT_JCYJ'
    # pwd = 'JIANCEyujing123!'
    # 序列化格式
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"times={times}&"
        f"dataFormat={dataFormat}&order_by={order_by}"
    )

    # 生成时间戳和uuid，并拼接接口url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'times': times,
                  'dataFormat': dataFormat,
                  'orderby': order_by,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接sign
    baseUrl += '&sign=' + sign
    print(baseUrl)
    # 当前浏览器打开新标签
    # webbrowser.open_new_tab(baseUrl)
    res = send_get_request_requests(baseUrl)
    print(res)


# 按时间范围和行政区划查询气象站日数据
def getSurfEleInRegionByTimeRange(adminCodes='120000', time_range='[20250727000000,20250727000000]'):
    elements = 'Station_Id_C,Station_Name,PRE_Time_0808,Datetime'
    data_code = 'SURF_CHN_MUL_DAY'
    interface_id = 'getSurfEleInRegionByTimeRange'
    # 服务节点
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    # 接口服务端IP和端口
    serviceIp = '10.226.90.120'
    # 用户名&密码
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"timeRange={time_range}&"
        f"adminCodes={adminCodes}&dataFormat={dataFormat}"
    )

    # 生成时间戳和uuid，并拼接接口url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'timeRange': time_range,
                  'adminCodes': adminCodes,
                  'dataFormat': dataFormat,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接sign
    baseUrl += '&sign=' + sign
    # 当前浏览器打开新标签
    # webbrowser.open_new_tab(baseUrl)
    res = send_get_request_requests(baseUrl)
    # with open(r"/home/ev/haiheliuyubaoyuagent/haihe-weather-analyzer-mcp/utils/test.json", "w", encoding='utf-8') as f:
    #     f.write(res)
    # print(res)
    return json.loads(res)['DS']


# ... existing code ...

def stasMaxRainByAreaAndTimeRange(adminCodes='120000', time_range='[20250101000000,20251231235959]', adminLevel=1):
    """
    统计区域内最大日降雨量

    Args:
        adminCodes: 行政区划代码
        time_range: 时间范围
        adminLevel: 行政区划级别（1-省，2-市，3-县）
    """
    # 根据行政区划级别定义不同的 elements
    if adminLevel == 1:
        # 省级
        elements = 'Province,Datetime'
    elif adminLevel == 2:
        # 市级
        elements = 'City,Province,Datetime'
    elif adminLevel == 3:
        # 县级
        elements = 'Cnty,City,Province,Datetime'
    else:
        # 默认省级
        elements = 'Province,Datetime'
    statEles = 'MAX_PRE_Time_0808'
    eleValueRanges = 'PRE_Time_0808:(,9999)'
    data_code = 'SURF_CHN_MUL_DAY'

    interface_id = 'statSurfEleInRegion'
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    serviceIp = '10.226.90.120'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"timeRange={time_range}&"
        f"statEles={statEles}&"
        f"eleValueRanges={eleValueRanges}&"
        f"adminCodes={adminCodes}&"
        f"dataFormat={dataFormat}"
    )

    # 生成时间戳和 uuid，并拼接接口 url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'timeRange': time_range,
                  'statEles': statEles,
                  'adminCodes': adminCodes,
                  'eleValueRanges': eleValueRanges,
                  'dataFormat': dataFormat,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成 sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接 sign
    baseUrl += '&sign=' + sign
    logger.info(baseUrl)
    res = send_get_request_requests(baseUrl)

    # logger.info(res)
    datas = json.loads(res)['DS']
    # 筛选 datas 中 MAX_PRE_Time_0808 最大的一条记录
    max_record = max(datas, key=lambda x: float(x['MAX_PRE_Time_0808']) if x['MAX_PRE_Time_0808'] else 0)
    if max_record:
        datetime_val = max_record.get('Datetime')

        if datetime_val:
            time_range_single = (f'[{datetime_val.replace(" ", "").replace(":", "").replace("-", "")}'
                                 f',{datetime_val.replace(" ", "").replace(":", "").replace("-", "")}]')

            single_day_datas = getSurfEleInRegionByTimeRange(adminCodes=adminCodes, time_range=time_range_single)

            filtered_data = [
                record for record in single_day_datas
                if record.get('Datetime') == datetime_val and record.get('PRE_Time_0808') == max_record.get(
                    'MAX_PRE_Time_0808')
            ]

            if filtered_data:
                max_record['Station_Id_C'] = filtered_data[0].get('Station_Id_C')
                max_record['Station_Name'] = filtered_data[0].get('Station_Name')

    return max_record
    # return {"Province": "天津市", "Datetime": "2025-09-01 00:00:00", "MAX_PRE_Time_0808": "222"}


def getSevpEleByTimeRangeHistory(time_range='[20260317000000,20260317000000]',
                                 elements='Datetime,V_AREA_ID,V_RAIN_24H'):
    """
        海河流域子分区实况面雨量查询
    """
    data_code = 'SURF_HHLY_AREA_RAIN_HOUR'

    interface_id = 'getSurfEleByTimeRange'
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    serviceIp = '10.226.90.120'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"timeRange={time_range}&"
        f"dataFormat={dataFormat}"
    )

    # 生成时间戳和 uuid，并拼接接口 url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'timeRange': time_range,
                  'dataFormat': dataFormat,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成 sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接 sign
    baseUrl += '&sign=' + sign
    logger.info(baseUrl)
    res = send_get_request_requests(baseUrl)
    try:
        payload = json.loads(res)
    except Exception as e:
        raise RuntimeError(f"面雨量接口返回不是 JSON: {res[:500]}") from e

    return_code = payload.get("returnCode") if isinstance(payload, dict) else None
    return_msg = str(payload.get("returnMessage", "")) if isinstance(payload, dict) else ""
    if return_code and return_code != "0":
        # returnCode=-1 且明确无记录时，视为空数据而非异常
        if str(return_code) == "-1" and "no record" in return_msg.lower():
            return []
        raise RuntimeError(
            f"面雨量接口错误: returnCode={return_code}, "
            f"message={return_msg}"
        )

    datas = payload.get("DS") if isinstance(payload, dict) else None
    if datas is None:
        raise RuntimeError(f"面雨量接口未返回 DS 字段: {payload}")
    return datas


def statSevpEleByTimeRangeHistory(time_range='[20260301000000,20260318000000]',
                                  elements='V_AREA_ID,Datetime'):
    """
        海河流域子分区实况面雨量统计  todo 不能分组时间字段
    """
    data_code = 'SURF_HHLY_AREA_RAIN_HOUR'
    statEles = 'SUM_V_RAIN_1H'
    interface_id = 'statSurfEle'
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    serviceIp = '10.226.90.120'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"statEles={statEles}&"
        f"timeRange={time_range}&"
        f"dataFormat={dataFormat}"
    )

    # 生成时间戳和 uuid，并拼接接口 url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'statEles': statEles,
                  'timeRange': time_range,
                  'dataFormat': dataFormat,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成 sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接 sign
    baseUrl += '&sign=' + sign
    logger.info(baseUrl)
    res = send_get_request_requests(baseUrl)
    try:
        payload = json.loads(res)
    except Exception as e:
        raise RuntimeError(f"面雨量统计接口返回不是 JSON: {res[:500]}") from e

    return_code = payload.get("returnCode") if isinstance(payload, dict) else None
    return_msg = str(payload.get("returnMessage", "")) if isinstance(payload, dict) else ""
    if return_code and return_code != "0":
        if str(return_code) == "-1" and "no record" in return_msg.lower():
            return []
        raise RuntimeError(
            f"面雨量统计接口错误: returnCode={return_code}, "
            f"message={return_msg}"
        )

    datas = payload.get("DS") if isinstance(payload, dict) else None
    if datas is None:
        raise RuntimeError(f"面雨量统计接口未返回 DS 字段: {payload}")
    return datas


def getSevpEleByTime(times='20250722080000',
                     elements='Datetime,V_AREA_ID,V_RAIN_24H'):
    """
    按具体时间检索海河流域子分区面雨量（如取某日 08:00 的 V_RAIN_24H）。
    """
    data_code = 'SURF_HHLY_AREA_RAIN_HOUR'
    interface_id = 'getSurfEleByTime'
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    serviceIp = '10.226.90.120'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"times={times}&"
        f"dataFormat={dataFormat}"
    )

    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {
        'serviceNodeId': serviceNodeId,
        'dataCode': data_code,
        'userId': userId,
        'interfaceId': interface_id,
        'elements': elements,
        'times': times,
        'dataFormat': dataFormat,
        'timestamp': timestamp,
        'nonce': nonce,
        "pwd": pwd
    }
    sign = getSign(signParams)
    if sign == "":
        print("generate sign is None")
    baseUrl += '&sign=' + sign
    logger.info(baseUrl)
    res = send_get_request_requests(baseUrl)
    try:
        payload = json.loads(res)
    except Exception as e:
        raise RuntimeError(f"面雨量接口返回不是 JSON: {res[:500]}") from e

    return_code = payload.get("returnCode") if isinstance(payload, dict) else None
    return_msg = str(payload.get("returnMessage", "")) if isinstance(payload, dict) else ""
    if return_code and return_code != "0":
        if str(return_code) == "-1" and "no record" in return_msg.lower():
            return []
        raise RuntimeError(
            f"面雨量接口错误: returnCode={return_code}, "
            f"message={return_msg}"
        )

    datas = payload.get("DS") if isinstance(payload, dict) else None
    if datas is None:
        raise RuntimeError(f"面雨量接口未返回 DS 字段: {payload}")
    return datas


def getSevpEleByTimeRangeForecast(time_range='[20260304080000,20260304120000]'):
    """
        海河流域子分区实况面雨量查询
    """
    elements = 'Datetime,V_AGING,V_MODE,V_INTVAL,V_AREA_ID,V_RAIN_DATA'
    data_code = 'SEVP_TJ_STAT_REGI_HHLY'

    interface_id = 'getSevpEleByTimeRange'
    serviceNodeId = 'NMIC_MUSIC_CMADAAS'
    serviceIp = '10.226.90.120'
    userId = 'BETJ_QXT_LYGXPT'
    pwd = 'Qxtly@2022ww'
    dataFormat = 'json'

    baseUrl = (
        f"http://{serviceIp}/music-ws/api?"
        f"serviceNodeId={serviceNodeId}&dataCode={data_code}&"
        f"userId={userId}&interfaceId={interface_id}&"
        f"elements={elements}&"
        f"timeRange={time_range}&"
        f"dataFormat={dataFormat}"
    )

    # 生成时间戳和 uuid，并拼接接口 url
    timestamp = str(int((time.time() * 1000)))
    nonce = str(uuid.uuid1())
    baseUrl += '&timestamp=' + timestamp
    baseUrl += '&nonce=' + nonce
    signParams = {'serviceNodeId': serviceNodeId,
                  'dataCode': data_code,
                  'userId': userId,
                  'interfaceId': interface_id,
                  'elements': elements,
                  'timeRange': time_range,
                  'dataFormat': dataFormat,
                  'timestamp': timestamp,
                  'nonce': nonce,
                  "pwd": pwd
                  }
    # 生成 sign
    sign = getSign(signParams)
    if (sign == ""):
        print("generate sign is None")
    # 拼接 sign
    baseUrl += '&sign=' + sign
    logger.info(baseUrl)
    res = send_get_request_requests(baseUrl)
    with open(r"/home/ev/haiheliuyubaoyuagent/haihe-weather-analyzer-mcp/utils/test.json", "w", encoding='utf-8') as f:
        f.write(res)
    return res  # logger.info(res)
    # datas = json.loads(res)['DS']


if __name__ == '__main__':
    # product_vector_point(fr'D:\gzt\天津气象台流域升级\数据\rain4AI/20250723000000-20250724000000.json', )
    # r'D:\gzt\天津气象台流域升级\数据\haihe_border_simple.shp')
    # get_pre_1h()
    # getSurfEleByTime()
    # print(stasMaxRainByAreaAndTimeRange())
    # getSurfEleInRegionByTimeRange()
    # getSevpEleByTimeRangeHistory('[20260317150000,20260317150000]')
    # print(statSevpEleByTimeRangeHistory())
    print(getSevpEleByTimeRangeForecast())
