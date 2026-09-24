import sys
import os
sys.path.append(os.getcwd())
from app.db.session import SessionLocal
from app.models.booking import Booking

db = SessionLocal()
b = db.query(Booking).filter(Booking.id == 'd60ea169-5ae0-46bc-ae2f-b42f5e6ada7d').first()
if b:
    print(f'Booking event_date: {b.event_date}')
    print(f'Calendar Event ID: {b.calendar_event_id}')
else:
    print('Not found')
