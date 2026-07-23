import hashlib
import math

import geopandas
import numpy as np
import pandas as pd
from elasticsearch import Elasticsearch, helpers
from tqdm import tqdm


def read_shp(filepath):


    point_df = geopandas.read_file(filepath)

    point_df['address'] = point_df['省份'] + point_df['城市'] + point_df['区域']

    point_df.rename(columns={'名称': 'name','大类':'category_1','中类':'category_2'}, inplace=True)

    point_df.drop(columns=['经度', '纬度', '省份', '城市', '区域'],inplace=True)

    print(point_df.head())
    print(point_df.columns)

    return point_df


def init_es_index():
    ES_HOST = "http://10.226.107.130:9200"

    INDEX_NAME = "poi_points"

    es = Elasticsearch(
        [ES_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True
    )

    mapping = {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 0
        },
        "mappings": {
            "dynamic": True,
            "properties": {
                "poi_id": {"type": "keyword"},
                "name": {
                  "type": "text",
                  "analyzer": "ik_max_word",
                  "search_analyzer": "ik_smart",
                  "fields": {
                    "keyword": {
                      "type": "keyword",
                      "ignore_above": 256
                    }
                  }
                },
                "category_1": {"type": "keyword"},
                "category_2": {"type": "keyword"},
                "address": {
                    "type": "text",
                    "analyzer": "ik_max_word",
                    "search_analyzer": "ik_smart",
                    "fields": {
                        "keyword": {
                            "type": "keyword",
                            "ignore_above": 512
                        }
                    }
                },
                "location": {"type": "geo_point"}
            }
        }
    }

    if not es.indices.exists(index=INDEX_NAME):
        es.indices.create(index=INDEX_NAME, body=mapping)
        print(f"索引已创建: {INDEX_NAME}")
    else:
        print(f"索引已存在: {INDEX_NAME}")


def clean_value(value):
    """把 pandas/numpy 类型转成 ES 可 JSON 序列化的普通类型"""
    if value is None:
        return None

    # pandas 缺失值
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, (np.integer,)):
        return int(value)

    if isinstance(value, (np.floating,)):
        if math.isnan(float(value)):
            return None
        return float(value)

    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()

    return value


def make_doc_id(row, idx, source_name, lon, lat):
    """
    生成稳定且不易冲突的 ES 文档 ID。

    注意：
    如果你之前导入旧 shp 时 _id 用的是 0、1、2 这种行号，
    新 shp 继续用行号就会冲突。
    所以这里加上 source_name 前缀。
    """

    # 如果 shp 里有唯一 ID 字段，优先使用
    candidate_id_fields = [
        "poi_id",
        "POI_ID",
        "id",
        "ID",
        "OBJECTID",
        "FID"
    ]

    for field in candidate_id_fields:
        if field in row and row[field] is not None and not pd.isna(row[field]):
            return f"{source_name}_{field}_{row[field]}"

    # 如果没有唯一 ID，则用 source_name + 行号 + 名称 + 坐标生成 md5
    name = row.get("name", "") if "name" in row else ""

    raw = f"{source_name}_{idx}_{name}_{lon:.7f}_{lat:.7f}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def generate_actions(gdf, index_name,source_name):
    for idx, row in tqdm(gdf.iterrows(), total=len(gdf), desc="生成 bulk 数据"):
        geom = row.geometry

        lon = float(geom.x)
        lat = float(geom.y)

        # 防止经纬度反了、坐标系没转、投影坐标误入
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            print(f"跳过异常坐标: idx={idx}, lon={lon}, lat={lat}")
            continue

        source = {}

        for col in gdf.columns:
            if col == "geometry":
                continue
            source[col] = clean_value(row[col])

        # ES geo_point：推荐用 {"lat": 纬度, "lon": 经度}
        source["location"] = {
            "lat": lat,
            "lon": lon
        }

        # 可选：保留原始经纬度字段，方便排查
        # source["longitude"] = lon
        # source["latitude"] = lat

        doc_id = make_doc_id(row, idx, source_name, lon, lat)

        yield {
            "_op_type": "index",
            "_index": index_name,
            "_id": doc_id,
            "_source": source
        }

def insert_es():
    ES_HOST = "http://10.226.107.130:9200"

    INDEX_NAME = "poi_points"

    es = Elasticsearch(
        [ES_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True
    )

    # gdf = read_shp(r'C:\Users\jzh\Desktop\天津市\天津市POI_2022.shp')
    gdf = read_shp(r'C:\Users\jzh\Desktop\北京市2022\北京POI_2022.shp')
    # gdf = read_shp(r'C:\Users\jzh\Desktop\河北省2022\河北省POI_2022.shp')
    success, errors = helpers.bulk(
        es,
        generate_actions(gdf, INDEX_NAME,"bj"),
        chunk_size=1000,
        request_timeout=120,
        raise_on_error=False
    )

    print("成功写入数量:", success)
    print("错误数量:", len(errors))

    if errors:
        print("前 3 条错误:")
        for e in errors[:3]:
            print(e)


def del_index():
    es = Elasticsearch(
        ["http://10.226.107.130:9200"]# 没有密码就删掉这一行
    )

    INDEX_NAME = "poi_points"

    if es.indices.exists(index=INDEX_NAME):
        es.indices.delete(index=INDEX_NAME)
        print(f"索引已删除: {INDEX_NAME}")
    else:
        print(f"索引不存在: {INDEX_NAME}")


if __name__ == '__main__':
    # read_shp(r'C:\Users\jzh\Desktop\天津市\天津市POI_2022.shp')
    # init_es_index()
    # insert_es()

    # del_index()




    ES_HOST = "http://10.226.107.130:9200"

    INDEX_NAME = "poi_points"

    es = Elasticsearch(
        [ES_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True
    )
    es.indices.refresh(index=INDEX_NAME)

    count_result = es.count(index=INDEX_NAME)
    print("ES 中文档数量:", count_result["count"])