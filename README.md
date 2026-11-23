# Backend do Gerenciador de TCCs da UFVJM

Grupo:

- Ana Carolina Pereira dos Santos
- Ana Júlia Oliveira Araújo
- Camila Santos Barbosa
- Deivid Camargos Batista
- Filipe Hipólito Diniz Durães

Professor: Alessandro Vivas Andrade

Disciplina: Sistemas Distribuídos

## Visão geral

Backend do Gerenciador de TCCs da UFVJM. O aluno envia o PDF, o orientador dá o parecer, a banca julga e a coordenação publica o trabalho aprovado no acervo público. A interface fica no repositório `thesis-manager-ufvjm-frontend`.

O sistema cuida de:

- submissão com PDF, validação e SHA-256;
- parecer do orientador e agendamento de bancas;
- avaliação dos membros e fechamento do resultado;
- publicação no repositório público e relatórios para a coordenação.

## Autenticação

O projeto não mantém login com senha, hash de senha nem recuperação de senha. Essa é uma decisão de política. A credencial é conferida pelo Google Workspace institucional e o backend recebe a sessão já autenticada, sem guardar dados sensíveis que a UFVJM já mantém no provedor.

O login usa django-allauth em modo headless. O backend valida o domínio institucional, o email_verified e o hd devolvidos pelo Google. O primeiro login cria o usuário local com o papel de aluno. Os papéis seguintes vêm das tabelas do banco, nunca do cliente.

Quando não há credenciais do Google disponíveis, o modo de teste com DEBUG=True libera o login de desenvolvimento descrito mais adiante.

## Tecnologias

Python 3.12, Django 6.1, Django REST Framework 3.18 e django-allauth 65. PostgreSQL 16 guarda os dados, Redis 7 cuida de sessão e cache, e RabbitMQ 4.1 move os eventos. O texto dos PDFs sai com pypdf e os relatórios em PDF saem com ReportLab. O frontend usa React 19 e fica em outro repositório.

## Arquitetura

Cada processo sai da mesma imagem Django. Cada app grava no banco do seu serviço por meio de `thesismanagerbackend.routers.ServiceDatabaseRouter`. Quando um processo precisa de um id de outro serviço, ele faz uma consulta separada e filtra a coluna local.

O diagrama mostra as peças e onde cada uma entra. Azul são a API e os serviços. Âmbar são os bancos e o Redis. Roxo é a fila de eventos.

![Arquitetura do backend: entrada, serviços, bancos PostgreSQL, Redis, RabbitMQ e consumidores](readme/arquitetura.png)

| Peça | Papel |
|---|---|
| `backend` | API principal, com `/api/v1`, `/admin/` e `/health`. |
| Serviços | Autenticação, monografias, bancas, repositório, notificações e relatórios. Cada um tem API e banco próprios. |
| `gateway` | nginx que distribui as chamadas entre as APIs no perfil `microservices`. |
| Eventos | A outbox alimenta o RabbitMQ. Consumidores enviam e-mail, extraem texto, fecham bancas e atualizam os bancos de leitura. |
| `init` | Cria os bancos, aplica migrações e declara as filas na primeira subida. |

## Rodando o projeto

### 1. Preparar o Linux

Os comandos são para Ubuntu, Debian ou Linux Mint.

```bash
sudo apt update
sudo apt install -y git curl ca-certificates docker.io docker-compose-plugin
sudo usermod -aG docker "$USER"
```

Saia da sessão e entre de novo, ou rode `newgrp docker`, para o grupo valer. Se `docker compose` ainda pedir senha, use `sudo` na frente dos comandos. Para não digitar a senha, libere só o binário do Docker:

```bash
echo "$(whoami) ALL=(root) NOPASSWD: /usr/bin/docker" | sudo tee /etc/sudoers.d/docker-nopass
sudo chmod 440 /etc/sudoers.d/docker-nopass
sudo visudo -c
```

### 2. Colocar os arquivos no lugar

Clone o backend e o frontend lado a lado. O Compose monta o frontend a partir de `../thesis-manager-ufvjm-frontend`.

```bash
git clone https://github.com/filipeduraes/thesis-manager-ufvjm-backend.git
cd thesis-manager-ufvjm-backend
cd ..
git clone https://github.com/filipeduraes/thesis-manager-ufvjm-frontend.git
cd thesis-manager-ufvjm-backend
```

Coloque o arquivo `.env` que acompanha a entrega na raiz do backend. Ele já vem com as credenciais do Google. Se você não recebeu esse arquivo, use o modelo:

```bash
cp .env.example .env
```

### 3. Subir backend e frontend juntos

Este é o caminho mais simples para ver o sistema funcionando:

```bash
docker compose --profile frontend up --build
```

A aplicação abre em `http://localhost:3000`. A API fica em `http://localhost:8000`. O endpoint de saúde é `http://localhost:8000/health`.

Na primeira subida, o Compose espera PostgreSQL, Redis, RabbitMQ e Mailpit ficarem prontos. Depois o serviço `init` aplica as migrações e declara as filas. Só então sobem o backend e os consumidores.

Os e-mails de desenvolvimento ficam no Mailpit, em `http://localhost:8025`. Nada é entregue para fora da máquina.

### 4. Rodar só o backend

Se você quiser usar o frontend que já está rodando na sua máquina, suba apenas o backend:

```bash
docker compose up --build
```

Comandos que ajudam nesse momento:

```bash
docker compose logs -f event-publisher notification-consumer
docker compose exec -T backend python manage.py check
docker compose exec -T backend python manage.py verify_audit_chain
```

## Configuração

O `.env` que você recebeu já traz as credenciais do Google. Se precisar revisar algum valor, estas são as variáveis mais usadas:

| Variável | Para que serve |
|---|---|
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Login institucional. Sem eles, o provedor Google não é registrado. |
| `DJANGO_SECRET_KEY` | Pode ficar vazia no uso comum. Defina um valor fixo antes de usar o perfil `microservices`. |
| `DEBUG` | Padrão `False`. Com `True`, libera o login de teste descrito mais adiante. |
| `DATABASE_URL` e as outras `*_DATABASE_URL` | Bancos por serviço. O Compose monta as seis URLs. |
| `REDIS_URL`, `RABBITMQ_URL` | Usadas quando os processos rodam fora do Compose. |
| `ADMIN_ALLOWED_CIDRS`, `TRUSTED_PROXY_COUNT`, `TRUSTED_PROXY_CIDRS` | Origem de rede da área administrativa. |
| `BACKEND_PORT`, `FRONTEND_PORT`, `GATEWAY_PORT` | Portas publicadas. Os padrões são 8000, 3000 e 8080. |

O Compose também publica PostgreSQL na 5433, RabbitMQ na 5672, o painel do RabbitMQ em `http://localhost:15672` e o Mailpit em `http://localhost:8025`. Todas as portas ficam em `127.0.0.1`.

Para login com Google, cadastre `http://localhost:8000/accounts/google/login/callback/` no Google Cloud Console. O allauth monta o `redirect_uri` a partir do `Host` da requisição, então use `localhost` na API.

## Outros modos de execução

### Entrar sem Google

Se você não tem as credenciais do Google Workspace, suba o stack com `DEBUG=True` na linha de comando:

```bash
DEBUG=True docker compose --profile frontend up --build
```

Crie uma sessão de teste com papel de orientador:

```bash
curl -i -c cookies.txt -X POST http://localhost:8000/api/v1/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"name":"Orientador Teste","email":"orientador@ufvjm.edu.br","roles":["ADVISOR"]}'
```

Os papéis aceitos são `ADMIN`, `COORDINATOR`, `ADVISOR` e `STUDENT`. Com `roles: ["ADMIN"]`, o mesmo cookie serve para `/admin/` no navegador. A rota de login de teste deixa de existir quando `DEBUG` é `False`.

### Rodar sem Docker

Em versões mais antigas do Ubuntu e do Mint, o `apt` traz PostgreSQL 14. Para garantir a versão 16, use o repositório oficial:

```bash
sudo apt install -y postgresql-common python3 python3-venv python3-pip
sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt install -y postgresql-16 redis-server rabbitmq-server
sudo systemctl enable --now postgresql redis-server rabbitmq-server
```

No Linux Mint, se o script da PGDG não reconhecer a distro, instale o pacote `postgresql-16` do repositório Ubuntu equivalente.

Crie o usuário e o banco principal. O papel precisa de `CREATEDB`, porque o projeto cria cinco bancos de serviço:

```bash
sudo -u postgres psql -c "CREATE ROLE thesis_manager_user WITH LOGIN CREATEDB PASSWORD 'local-development-only';"
sudo -u postgres psql -c "CREATE DATABASE thesis_manager OWNER thesis_manager_user;"
```

Configure o RabbitMQ com o usuário e o vhost do `.env.example`:

```bash
sudo rabbitmqctl add_user thesis_events change-me
sudo rabbitmqctl add_vhost thesis_manager
sudo rabbitmqctl set_permissions -p thesis_manager thesis_events ".*" ".*" ".*"
```

Ajuste as seis URLs de banco no `.env` e ligue a separação de bancos:

```dotenv
DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_manager
AUTH_DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_auth
COMMITTEE_DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_committee
REPOSITORY_DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_repository
REPORTS_DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_reports
NOTIFICATIONS_DATABASE_URL=postgres://thesis_manager_user:local-development-only@localhost:5432/thesis_notifications
SERVICE_DATABASES_SHARED=False
```

Instale as dependências, crie os bancos de serviço e aplique as migrações:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py ensure_service_databases
for alias in auth default committee repository reports notifications; do python manage.py migrate --database "$alias" --noinput; done
python manage.py declare_event_topology
```

Inicie a API:

```bash
python manage.py runserver
```

Os consumidores podem rodar em outros terminais:

```bash
python manage.py publish_outbox_events
python manage.py consume_notifications
python manage.py consume_indexing_events
python manage.py consume_adjudication_events
python manage.py consume_repository_events
python manage.py consume_reports_events
```

### Rodar as APIs separadas

Cada serviço tem sua própria URLconf. Para subir as seis APIs e o gateway nginx:

```bash
DJANGO_SECRET_KEY=escolha-uma-chave-fixa docker compose --profile microservices up --build
```

As APIs ficam nas portas 8011 a 8016. O gateway usa a 8080 porque o `backend` já ocupa a 8000. Os PDFs ficam no volume nomeado `media_data`. O comando `docker compose down -v` apaga bancos, filas, cache e uploads.

## Testes

Crie o ambiente virtual antes de rodar qualquer suíte, mesmo se você subiu o projeto com Compose:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Comece pela suíte rápida. Ela cobre regras de negócio, permissões e contratos principais:

```bash
python manage.py test_fast
```

Depois rode a suíte completa. Leva cerca de oito minutos e ainda tem seis falhas conhecidas no SQLite, ligadas a diferenças do banco. Para ver tudo passando, use o `test-postgres.sh` mais adiante:

```bash
python manage.py test
```

Para os testes da interface, instale as dependências do frontend e o navegador uma vez:

```bash
cd ../thesis-manager-ufvjm-frontend
npm install
npx playwright install chromium
npm run test:e2e
```

Se quiser validar as regras em PostgreSQL dividido, use o banco do Compose:

```bash
cd ../thesis-manager-ufvjm-backend
./scripts/test-postgres.sh
```

## Exemplos de API

Rotas públicas:

```bash
curl http://localhost:8000/api/v1/session
curl 'http://localhost:8000/api/v1/public/theses?q=sistemas'
curl http://localhost:8000/api/v1/public/courses
```

Rotas autenticadas precisam do cookie. Escritas também precisam do CSRF. Com `DEBUG=True`, crie uma sessão de aluno e este comando responde:

```bash
curl -c cookies-aluno.txt -X POST http://localhost:8000/api/v1/auth/dev-login \
  -H 'Content-Type: application/json' \
  -d '{"name":"Aluno Teste","email":"aluno@ufvjm.edu.br","roles":["STUDENT"]}'
curl -b cookies-aluno.txt http://localhost:8000/api/v1/theses/me
```

O fluxo do aluno começa em `PUT /api/v1/theses/me`, segue com `POST /api/v1/theses/me/files` e termina em `POST /api/v1/theses/me/submit`. O relatório da coordenação fica em `GET /api/v1/reports/overview`.

## Estrutura do diretório

```text
accounts/                 usuário, papéis, sessão, auditoria, diretórios
academic/                 cursos, departamentos e vínculos
theses/                   monografias, bancas, avaliações, upload, repositório
events/                   catálogo, outbox, publicador, topologia RabbitMQ
notifications/            templates, destinatários, consumidor e convites externos
indexing/                 extração de texto e índice de busca
adjudication/             cálculo assíncrono do resultado da banca
repository_readmodel/     banco de leitura do repositório público
reports/                  indicadores e exportações PDF/CSV
reports_readmodel/        banco de leitura dos relatórios
thesismanagerbackend/     configuração, roteador de bancos, URLs e utilitários
docker/, scripts/         infraestrutura e scripts de teste
```
