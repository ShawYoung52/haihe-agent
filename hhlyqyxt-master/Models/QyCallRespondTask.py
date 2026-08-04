from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, SmallInteger, String, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyCallRespondTask(Base):
    __tablename__ = 'qy_call_respond_task'

    id = Column(Integer, primary_key=True)
    emergency_monitor_id = Column(Integer)
    response_level = Column(SmallInteger, nullable=False, default=0)
    datatime = Column(DateTime)
    impact_city = Column(String(512))
    status = Column(String(20), nullable=False, default='pending')
    report_docx_path = Column(String(512))
    report_pdf_path = Column(String(512))
    confirm_person = Column(String(64))
    confirm_time = Column(DateTime)
    send_time = Column(DateTime)
    create_time = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyCallRespondTask(id={self.id}, status={self.status})"


"""
CREATE TABLE IF NOT EXISTS qy_call_respond_task (
    id SERIAL PRIMARY KEY,
    emergency_monitor_id INTEGER,
    response_level SMALLINT NOT NULL DEFAULT 0,
    datatime TIMESTAMP,
    impact_city VARCHAR(512),
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    report_docx_path VARCHAR(512),
    report_pdf_path VARCHAR(512),
    confirm_person VARCHAR(64),
    confirm_time TIMESTAMP,
    send_time TIMESTAMP,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_status ON qy_call_respond_task(status);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_task_emergency_monitor_id ON qy_call_respond_task(emergency_monitor_id);
"""