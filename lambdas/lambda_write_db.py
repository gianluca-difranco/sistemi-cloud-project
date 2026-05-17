import boto3
import os
import json

# Recuperiamo gli ARN necessari dalle variabili d'ambiente
CLUSTER_ARN = os.environ['CLUSTER_ARN']
SECRET_ARN = os.environ['SECRET_ARN']
DB_NAME = os.environ.get('DB_NAME', 'mydb')

rds_client = boto3.client('rds-data')

def execute_sql(sql_statement, parameters=[]):
    """Esegue una query SQL tramite Data API"""
    return rds_client.execute_statement(
        secretArn=SECRET_ARN,
        resourceArn=CLUSTER_ARN,
        database=DB_NAME,
        sql=sql_statement,
        parameters=parameters
    )

def lambda_handler(event, context):
    try:
        # 1. Creazione tabella 'message' se non esiste
        create_table_query = """
        CREATE TABLE IF NOT EXISTS message (
            id SERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
        execute_sql(create_table_query)

        # 2. Processo messaggi da SQS
        for record in event['Records']:
            message_body = record['body']
            try:
                data = json.loads(message_body)
                if isinstance(data, dict) and 'content' in data:
                    message_content = data['content']
                else:
                    message_content = message_body
            except json.JSONDecodeError:
                message_content = message_body
            
            insert_query = "INSERT INTO message (content) VALUES (:content)"
            # Con Data API i parametri si passano così (più sicuro contro SQL Injection)
            sql_parameters = [
                {'name': 'content', 'value': {'stringValue': message_content}}
            ]
            
            execute_sql(insert_query, sql_parameters)
            print(f"Inserito: {message_content}")

        return {'statusCode': 200, 'body': 'Successo'}

    except Exception as e:
        print(f"Errore: {str(e)}")
        raise e