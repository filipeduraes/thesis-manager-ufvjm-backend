# Thesis Manager UFVJM Backend

Backend do sistema de gerenciamento de Trabalhos de Conclusão de Curso (TCC), desenvolvido com Django e Django REST Framework.

## Requisitos

Em Debian, Ubuntu ou Linux Mint, instale os pacotes necessários:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git postgresql postgresql-contrib redis-server docker.io docker-compose-v2
```

Com Docker Compose, PostgreSQL, Redis e RabbitMQ serão iniciados em containers; os serviços instalados no sistema não são usados nesse modo.

Os comandos `docker compose` abaixo são executados sem `sudo`. Para isso, adicione seu usuário ao grupo `docker` uma única vez e abra uma nova sessão:

```bash
sudo usermod -aG docker "$USER"
```

## Configuração

Clone o backend e entre na pasta:

```bash
git clone https://github.com/filipeduraes/thesis-manager-ufvjm-backend.git
cd thesis-manager-ufvjm-backend
```

Copie `.env.example` para `.env` e configure `GOOGLE_CLIENT_ID` e `GOOGLE_CLIENT_SECRET` para habilitar o login Google:

```bash
cp .env.example .env
```

O arquivo `.env` contém valores locais e não deve ser enviado ao Git. Defina também a senha local do RabbitMQ em `RABBITMQ_DEFAULT_PASS` (só letras, números, `-` ou `_`) e repita a mesma senha em `RABBITMQ_URL`, usada quando o backend roda fora do Docker.

No Google OAuth, cadastre este endereço de retorno:

```text
http://localhost:8000/accounts/google/login/callback/
```

## Iniciar o backend com Docker Compose

O comando padrão inicia PostgreSQL, Redis, RabbitMQ, o backend e os workers de eventos. Não requer o repositório do frontend:

```bash
docker compose up --build
```

Ordem de inicialização:

1. `db`, `redis` e `rabbitmq` sobem e passam nos healthchecks.
2. `init` roda uma única vez `migrate` e `declare_event_topology` (exchange `thesis.events`, fila `notifications.thesis-events` e DLQ) e termina.
3. Só depois que `init` termina com sucesso sobem:
   - `backend`, em `http://localhost:8000`;
   - `event-publisher`, que publica no RabbitMQ os eventos gravados na outbox;
   - `notification-consumer`, que envia as notificações por e-mail. Em desenvolvimento usa o backend de console: os e-mails aparecem no log do serviço.

Para parar, pressione `Ctrl+C`; os dados permanecem nos volumes Docker. O painel do RabbitMQ fica em `http://localhost:15672`, com o usuário e a senha de `RABBITMQ_DEFAULT_USER` e `RABBITMQ_DEFAULT_PASS`.

Para acompanhar os eventos e as notificações:

```bash
docker compose logs -f event-publisher notification-consumer
```

Para iniciar também o frontend em `http://localhost:3000`, clone `thesis-manager-ufvjm-frontend` ao lado do repositório do backend e use:

```bash
docker compose --profile frontend up --build
```

Se o frontend já estiver rodando localmente com `npm start`, use o comando padrão do backend e mantenha o frontend local. Não inicie as duas cópias do frontend ao mesmo tempo na mesma porta.

## Executar sem Docker

Crie e ative o ambiente virtual, instale as dependências e inicie os serviços locais:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo systemctl enable --now postgresql redis-server
```

Crie um usuário e banco PostgreSQL:

```bash
sudo -u postgres psql
```

No `psql`, execute, usando uma senha forte:

```sql
CREATE ROLE thesis_manager_user WITH LOGIN PASSWORD 'escolha-uma-senha-forte';
CREATE DATABASE thesis_manager OWNER thesis_manager_user;
```

Configure `DATABASE_URL` no `.env` para esse usuário e banco:

```dotenv
DATABASE_URL=postgresql://thesis_manager_user:escolha-uma-senha-forte@localhost:5432/thesis_manager
```

Aplique as migrações e inicie o backend:

```bash
python manage.py migrate
python manage.py runserver
```

O backend estará disponível em `http://127.0.0.1:8000/`. Para iniciar o frontend, abra outro terminal na pasta `thesis-manager-ufvjm-frontend` e execute `npm start`.

Os eventos assíncronos precisam de um RabbitMQ acessível em `RABBITMQ_URL`. Com ele no ar, declare a topologia uma vez e inicie os workers, cada um em um terminal:

```bash
python manage.py declare_event_topology
python manage.py publish_outbox_events
python manage.py consume_notifications
```

Sem os workers, o backend continua funcionando: os eventos ficam pendentes na tabela `outbox_event` e são publicados quando o publicador subir.

## Comandos úteis

```bash
python manage.py createsuperuser
python manage.py check
python manage.py test
```

Operação dos eventos assíncronos (no Docker, prefixe com `docker compose exec -T backend`):

```bash
python manage.py publish_outbox_events --once                 # publica um único lote (depuração)
python manage.py publish_outbox_events --requeue-failed all   # devolve eventos FAILED para a fila
python manage.py consume_notifications --retry-pending        # reenvia e-mails FAILED ou presos em PENDING
```
