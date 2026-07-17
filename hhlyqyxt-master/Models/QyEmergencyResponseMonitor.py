from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, Numeric, SmallInteger, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyEmergencyResponseMonitor(Base):
    __tablename__ = 'qy_emergency_response_monitor'

    id = Column(Integer, primary_key=True)
    datatime = Column(DateTime, nullable=False, unique=True)
    minute_monitor_id = Column(Integer)
    total_national_stations = Column(Integer, nullable=False, default=0)
    station_12h_baoyu = Column(Integer, nullable=False, default=0)
    ratio_12h_baoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_baoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_baoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_dabaoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_dabaoyu = Column(Numeric(6, 4), nullable=False, default=0)
    station_24h_tedabaoyu = Column(Integer, nullable=False, default=0)
    ratio_24h_tedabaoyu = Column(Numeric(6, 4), nullable=False, default=0)
    response_level = Column(SmallInteger, nullable=False, default=0)
    create_time = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyEmergencyResponseMonitor(id={self.id}, datatime={self.datatime})"


"""
CREATE TABLE qy_emergency_response_monitor (
    id SERIAL PRIMARY KEY,
    datatime TIMESTAMP NOT NULL UNIQUE,
    minute_monitor_id INTEGER,
    total_national_stations INTEGER NOT NULL DEFAULT 0,
    station_12h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_12h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_baoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_baoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_dabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_dabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    station_24h_tedabaoyu INTEGER NOT NULL DEFAULT 0,
    ratio_24h_tedabaoyu NUMERIC(6,4) NOT NULL DEFAULT 0,
    response_level SMALLINT NOT NULL DEFAULT 0,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_qy_emergency_response_monitor_datatime
    ON qy_emergency_response_monitor(datatime DESC);
"""
