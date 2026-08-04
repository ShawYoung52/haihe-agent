from pydantic import ConfigDict
from sqlalchemy import Column, DateTime, Integer, String, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class QyCallRespondSendLog(Base):
    __tablename__ = 'qy_call_respond_send_log'

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, nullable=False)
    target_group = Column(String(128))
    status = Column(String(20), nullable=False, default='success')
    detail = Column(String(512))
    send_time = Column(DateTime, server_default=text('CURRENT_TIMESTAMP'))

    model_config = ConfigDict(from_attributes=True)

    def __repr__(self):
        return f"QyCallRespondSendLog(id={self.id}, task_id={self.task_id}, status={self.status})"


"""
CREATE TABLE IF NOT EXISTS qy_call_respond_send_log (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,
    target_group VARCHAR(128),
    status VARCHAR(20) NOT NULL DEFAULT 'success',
    detail VARCHAR(512),
    send_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qy_call_respond_send_log_task_id ON qy_call_respond_send_log(task_id);
"""