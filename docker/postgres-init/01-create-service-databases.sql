-- Um banco por serviço, conforme a fronteira de dados de cada API.
SELECT 'CREATE DATABASE thesis_auth' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'thesis_auth')\gexec
SELECT 'CREATE DATABASE thesis_committee' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'thesis_committee')\gexec
SELECT 'CREATE DATABASE thesis_notifications' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'thesis_notifications')\gexec
SELECT 'CREATE DATABASE thesis_repository' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'thesis_repository')\gexec
SELECT 'CREATE DATABASE thesis_reports' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'thesis_reports')\gexec
