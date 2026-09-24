with open('app/services/calendar_service.py', 'r', encoding='utf-8') as f:
    content = f.read()

old_try = '''    try:
        if booking.calendar_event_id:
            event = service.events().update(
                calendarId=calendar_id, 
                eventId=booking.calendar_event_id,
                body=event_body, 
                sendUpdates='none'
            ).execute()
            return event.get('id')
        else:
            event = service.events().insert(
                calendarId=calendar_id, 
                body=event_body, 
                sendUpdates='none'
            ).execute()
            booking.calendar_event_id = event.get('id')
            db.commit()
            return event.get('id')
    except Exception as e:
        logger.error(f"Error sincronizando evento en Google Calendar: {e}")
        return None'''

new_try = '''    try:
        from googleapiclient.errors import HttpError
        if booking.calendar_event_id:
            try:
                event = service.events().update(
                    calendarId=calendar_id, 
                    eventId=booking.calendar_event_id,
                    body=event_body, 
                    sendUpdates='none'
                ).execute()
                return event.get('id')
            except HttpError as he:
                if he.resp.status == 404 or he.resp.status == 410:
                    logger.warning(f"Evento {booking.calendar_event_id} no encontrado en Google Calendar. Creando uno nuevo.")
                    booking.calendar_event_id = None
                else:
                    raise he

        if not booking.calendar_event_id:
            event = service.events().insert(
                calendarId=calendar_id, 
                body=event_body, 
                sendUpdates='none'
            ).execute()
            booking.calendar_event_id = event.get('id')
            db.commit()
            return event.get('id')
    except Exception as e:
        logger.error(f"Error sincronizando evento en Google Calendar: {e}")
        return None'''

if old_try in content:
    content = content.replace(old_try, new_try)
    with open('app/services/calendar_service.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("Replaced!")
else:
    print("Old try block not found. Trying another way.")
