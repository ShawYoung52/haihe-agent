from pydantic import ConfigDict
from sqlalchemy import Column, String, create_engine,Integer,Float,DateTime
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import declarative_base

Base = declarative_base()
class QyHourMonitor(Base):
    __tablename__ = 'qy_hour_monitor'

    id = Column(Integer, primary_key=True)
    date_time = Column(DateTime)
    rain_station_sum = Column(Integer)
    station_sum = Column(Integer)

    max_rain_24h = Column(Float)
    rain_position = Column(String)

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyHourMonitor(id={self.id}, date_time={self.date_time})"