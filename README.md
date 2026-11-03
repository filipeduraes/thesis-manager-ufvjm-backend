# Thesis Manager UFVJM Backend

Backend do sistema de gerenciamento de Trabalhos de Conclusão de Curso (TCC), desenvolvido com Django e Django REST Framework.

## Requisitos

Em Debian, Ubuntu ou Linux Mint, instale os pacotes necessários:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git postgresql postgresql-contrib redis-server docker.io docker-compose-v2
```

Com Docker Compose, PostgreSQL e Redis serão iniciados em containers; os serviços instalados no sistema não são usados nesse modo.

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

O arquivo `.env` contém valores locais e não deve ser enviado ao Git.

No Google OAuth, cadastre este endereço de retorno:

```text
http://localhost:8000/accounts/google/login/callback/
```

## Iniciar o backend com Docker Compose

O comando padrão inicia PostgreSQL, Redis e backend. Não requer o repositório do frontend:

```bash
sudo docker compose up --build
```

O backend aplica as migrações automaticamente e fica disponível em `http://localhost:8000`. Para parar, pressione `Ctrl+C`; os dados permanecem nos volumes Docker.

Para iniciar também o frontend em `http://localhost:3000`, clone `thesis-manager-ufvjm-frontend` ao lado do repositório do backend e use:

```bash
sudo docker compose --profile frontend up --build
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

## Comandos úteis

```bash
python manage.py createsuperuser
python manage.py check
python manage.py test
```
