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
python manage.py consume_indexing_events                      # extrai texto do PDF e atualiza o índice de busca
python manage.py consume_adjudication_events                  # calcula o resultado da banca após a última avaliação
python manage.py consume_repository_events                    # projeta publicações no banco do repositório
python manage.py consume_reports_events                       # projeta fatos no banco de relatórios
python manage.py verify_audit_chain                           # verifica a integridade do log de auditoria
```

As filas `indexing.thesis-events` e `adjudication.thesis-events` (e suas DLQs) são declaradas por `declare_event_topology`. No Compose, os serviços `indexing-consumer` e `adjudication-consumer` rodam esses comandos.

### APIs por serviço

Além do gateway `backend` (URLconf completa), cada fronteira tem sua própria API quando o perfil `microservices` está ativo:

| Serviço | Porta | URLconf |
|---|---|---|
| Autenticação e usuários | 8011 | `thesismanagerbackend.service_urls.auth` |
| Monografias | 8012 | `thesismanagerbackend.service_urls.theses` |
| Bancas e avaliação | 8013 | `thesismanagerbackend.service_urls.committee` |
| Repositório público | 8014 | `thesismanagerbackend.service_urls.repository` |
| Notificações | 8015 | `thesismanagerbackend.service_urls.notifications` |
| Relatórios | 8016 | `thesismanagerbackend.service_urls.reports` |

```bash
docker compose --profile microservices up --build
```

Cada serviço tem seu próprio banco (`AUTH_DATABASE_URL`, `REPOSITORY_DATABASE_URL`, etc.). O Compose define `SERVICE_DATABASES_SHARED=False`, ativando a separação física. Fora do Compose (desenvolvimento/E2E) o padrão é `True`, com todos os aliases no mesmo SQLite/Postgres.

As leituras entre serviços foram convertidas para **snapshots locais** (`Thesis.advisor_name/course_name/course_code/main_author_name`, `Defense.thesis_snapshot`) e para consultas de tabela única roteadas ao banco dono; não há mais JOINs SQL entre bancos de serviços diferentes. `ServiceDatabaseRouter` está coberto por testes de separação.

**Solução de problemas:** se você subiu o Compose com versões anteriores do roteador, os bancos `thesis_*` podem conter registros em `django_migrations` sem as tabelas correspondentes (o erro `relation "auth_group" does not exist`). Como o ambiente é de desenvolvimento sem dados reais, recrie os volumes:

```bash
sudo docker compose down -v
sudo docker compose --profile frontend up --build
```
