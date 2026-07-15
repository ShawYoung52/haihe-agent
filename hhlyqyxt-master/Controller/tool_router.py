from elasticsearch import Elasticsearch
from fastapi import APIRouter
from sqlalchemy import desc

from Models.QyEmergencyResponseMonitor import QyEmergencyResponseMonitor
from utils.db import Session

toolrouter = APIRouter(
    prefix='/tool',
    tags=['tool']
)

@toolrouter.get('/search_poi')
def search_poi(keyword: str, size: int = 10):
    """
        根据 POI 名称查询最优结果：
        1. 先做精确匹配 name.keyword
        2. 如果没有，再做加权模糊匹配
    """

    ES_HOST = "http://10.226.107.130:9200"

    INDEX_NAME = "poi_points"

    es = Elasticsearch(
        [ES_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True
    )

    # 第一步：精确匹配
    exact_body = {
        "size": size,
        "query": {
            "term": {
                "name.keyword": keyword
            }
        }
    }

    exact_resp = es.search(index=INDEX_NAME, body=exact_body)
    exact_hits = exact_resp["hits"]["hits"]

    if exact_hits:
        return {
            "match_type": "exact",
            "rows": [exact_hits[0]],
            "hits": exact_hits
        }

    # 第二步：加权模糊匹配
    fuzzy_body = {
        "size": size,
        "_source": [
            "name",
            "category_1",
            "category_2",
            "address",
            "location",
            "longitude",
            "latitude"
        ],
        "query": {
            "bool": {
                "should": [
                    {
                        "match_phrase": {
                            "name": {
                                "query": keyword,
                                "boost": 30
                            }
                        }
                    },
                    {
                        "match": {
                            "name": {
                                "query": keyword,
                                "operator": "and",
                                "boost": 10
                            }
                        }
                    },
                    {
                        "match": {
                            "name": {
                                "query": keyword,
                                "boost": 1
                            }
                        }
                    }
                ],
                "minimum_should_match": 1
            }
        },
        "sort": [
            {
                "_score": {
                    "order": "desc"
                }
            }
        ]
    }

    fuzzy_resp = es.search(index=INDEX_NAME, body=fuzzy_body)
    fuzzy_hits = fuzzy_resp["hits"]["hits"]

    return {
        "match_type": "fuzzy",
        "rows": fuzzy_hits if fuzzy_hits else None,
        "hits": fuzzy_hits
    }

@toolrouter.get('/search_poi_by_dis')
def search_poi_by_dis(keyword: str,lon:float,lat:float, size: int = 10,distance: int = 10):
    body = {
      "size": size,
      "_source": [
        "name",
        "category_1",
        "category_2",
        "address",
        "location"
      ],
      "query": {
        "bool": {
          "filter": [
            {
              "geo_distance": {
                "distance": f"{distance}km",
                "location": {
                  "lat": lat,
                  "lon": lon
                }
              }
            }
          ],
          "should": [
            {
              "term": {
                "name.keyword": {
                  "value": keyword,
                  "boost": 100
                }
              }
            },
            {
              "match_phrase": {
                "name": {
                  "query": keyword,
                  "boost": 30
                }
              }
            },
            {
              "match": {
                "name": {
                  "query": keyword,
                  "operator": "and",
                  "boost": 10
                }
              }
            }
          ],
          "minimum_should_match": 1
        }
      },
      "sort": [
        {
          "_score": {
            "order": "desc"
          }
        },
        {
          "_geo_distance": {
            "location": {
              "lat": lat,
              "lon": lon
            },
            "order": "asc",
            "unit": "m",
            "distance_type": "arc"
          }
        }
      ]
    }

    ES_HOST = "http://10.226.107.130:9200"

    INDEX_NAME = "poi_points"

    es = Elasticsearch(
        [ES_HOST],
        timeout=60,
        max_retries=3,
        retry_on_timeout=True
    )
    fuzzy_resp = es.search(index=INDEX_NAME, body=body)
    fuzzy_hits = fuzzy_resp["hits"]["hits"]
    return {
        "match_type": "fuzzy",
        "rows": fuzzy_hits if fuzzy_hits else None,
        "hits": fuzzy_hits
    }


@toolrouter.get("/emergency-response/latest")
def get_latest_emergency_response(limit: int = 1):
    """返回最新的应急响应监测记录。"""
    if limit < 1 or limit > 100:
        limit = 1

    session = Session()
    try:
        rows = (
            session.query(QyEmergencyResponseMonitor)
            .order_by(desc(QyEmergencyResponseMonitor.datatime))
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "datatime": r.datatime.strftime("%Y-%m-%d %H:%M:%S") if r.datatime else None,
                "minute_monitor_id": r.minute_monitor_id,
                "total_national_stations": r.total_national_stations,
                "station_12h_baoyu": r.station_12h_baoyu,
                "ratio_12h_baoyu": float(r.ratio_12h_baoyu),
                "station_24h_baoyu": r.station_24h_baoyu,
                "ratio_24h_baoyu": float(r.ratio_24h_baoyu),
                "station_24h_dabaoyu": r.station_24h_dabaoyu,
                "ratio_24h_dabaoyu": float(r.ratio_24h_dabaoyu),
                "station_24h_tedabaoyu": r.station_24h_tedabaoyu,
                "ratio_24h_tedabaoyu": float(r.ratio_24h_tedabaoyu),
                "response_level": r.response_level,
                "create_time": r.create_time.strftime("%Y-%m-%d %H:%M:%S") if r.create_time else None,
            }
            for r in rows
        ]
    finally:
        session.close()