import boto3
import os
import json
import smtplib
from email.message import EmailMessage

SNS_TOPIC_ARN = os.environ.get('SNS_TOPIC_ARN')
sns_client = boto3.client('sns')

def lambda_handler(event, context):
    try:
        for record in event.get('Records', []):
            body = json.loads(record['body'])
            tenant_id = body.get('tenant_id')
            matchday = body.get('matchday')
            results = body.get('results', [])
            emails = body.get('emails', [])
            
            
            if not tenant_id or not matchday:
                print("Messaggio SQS non valido, mancano tenant_id o matchday")
                continue
            
            # Costruisci il messaggio
            testo_messaggio = f"Ciao, la giornata {matchday} è stata appena calcolata.\n\nEcco i risultati finali:\n"
            
            if not results:
                testo_messaggio += "Nessun risultato disponibile per questa giornata.\n"
            else:
                for m in results:
                    home = m.get('home_team', 'Riposo')
                    away = m.get('away_team', 'Riposo')
                    h_score = m.get('home_score', 0)
                    a_score = m.get('away_score', 0)
                    testo_messaggio += f"- {home} vs {away}: {h_score} - {a_score}\n"
            
            # Invio e-mail tramite SMTP (MailHog in locale)
            SMTP_HOST = os.environ.get('SMTP_HOST')
            if SMTP_HOST and emails:
                print(f"SMTP_HOST rilevato ({SMTP_HOST}). Invio a {len(emails)} utenti.")
                for email_dest in emails:
                    msg = EmailMessage()
                    msg.set_content(testo_messaggio)
                    msg['Subject'] = f"Risultati Giornata {matchday} - FantaCloud"
                    msg['From'] = "noreply@fantacloud.local"
                    msg['To'] = email_dest
                    
                    try:
                        with smtplib.SMTP(SMTP_HOST, 1025) as s:
                            s.send_message(msg)
                        print(f"Email inviata a {email_dest}")
                    except Exception as e:
                        print(f"Errore invio SMTP a {email_dest}: {e}")
            
            # Notifica globale via SNS (Produzione o LocalStack SNS)
            if SNS_TOPIC_ARN:
                try:
                    sns_client.publish(
                        TopicArn=SNS_TOPIC_ARN,
                        Message=testo_messaggio,
                        Subject=f"Risultati Giornata {matchday} - FantaCloud",
                        MessageAttributes={
                            'tenant_id': {
                                'DataType': 'String',
                                'StringValue': str(tenant_id)
                            }
                        }
                    )
                    print(f"Notifica SNS inviata per tenant {tenant_id}")
                except Exception as e:
                    print(f"Errore invio SNS: {e}")
                
        return {'statusCode': 200, 'body': 'Successo'}

    except Exception as e:
        print(f"Errore critico Lambda: {str(e)}")
        raise e
