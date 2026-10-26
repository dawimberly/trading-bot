try:
    from modules.alerts import send_email_alert
    print('send_email_alert import OK')
except Exception as e:
    print('send_email_alert import failed:', e)
