from pydantic import ConfigDict
from sqlalchemy import Integer, Column, Float, DateTime, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()
class QyMinuteMonitor(Base):
    __tablename__ = 'qy_minute_monitor'

    id = Column(Integer, primary_key=True)
    datatime = Column(DateTime)
    station_num = Column(Integer)
    rain_level_1 = Column(Integer)
    rain_level_2 = Column(Integer)
    rain_level_3 = Column(Integer)
    rain_level_4 = Column(Integer)
    rain_level_5 = Column(Integer)
    rain_level_6 = Column(Integer)

    tj_rain_level_1 = Column(Integer)
    tj_rain_level_2 = Column(Integer)
    tj_rain_level_3 = Column(Integer)
    tj_rain_level_4 = Column(Integer)
    tj_rain_level_5 = Column(Integer)
    tj_rain_level_6 = Column(Integer)

    mean_rain = Column(Float)
    tj_mean_rain = Column(Float)

    geojsonurl = Column(String)
    impact_city = Column(String)



    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyMinuteMonitor(id={self.id}, date_time={self.date_time})"