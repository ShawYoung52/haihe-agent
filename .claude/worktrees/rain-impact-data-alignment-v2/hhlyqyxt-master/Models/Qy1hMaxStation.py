from pydantic import ConfigDict
from sqlalchemy import Integer, DateTime, Column, Float, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()
class Qy1hMaxStation(Base):
    __tablename__ = 'qy_1h_max_station'

    id = Column(Integer, primary_key=True)
    lon = Column(Float)
    lat = Column(Float)
    station_id = Column(String)
    province = Column(String)
    city = Column(String)
    cnty = Column(String)
    station_name = Column(String)
    pre_1h = Column(Float)
    max_time = Column(DateTime)
    hour_monitor_id = Column(Integer)

    model_config = ConfigDict(from_attributes=True)