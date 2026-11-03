# Thesis Manager UFVJM Backend

Backend do sistema de gerenciamento de Trabalhos de Conclusão de Curso (TCC), desenvolvido com Django e Django REST Framework.

## Requisitos

Antes de começar, instale os seguintes componentes:

- Python 3
- pip
- venv
- Git
- PostgreSQL

Em distribuições baseadas em Ubuntu ou Linux Mint, execute:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
```

## 1. Obter o projeto

Clone o repositório e entre no diretório do projeto:

```bash
git clone https://github.com/filipeduraes/thesis-manager-ufvjm-backend.git
cd thesis-manager-ufvjm-backend
```

Caso já tenha o repositório clonado, entre diretamente na pasta existente.

## 2. Criar o ambiente virtual

Crie um ambiente virtual Python na raiz do projeto:

```bash
python3 -m venv .venv
```

O ambiente virtual isola as dependências do projeto das demais instalações Python do sistema.

## 3. Ativar o ambiente virtual

Execute:

```bash
source .venv/bin/activate
```

Após a ativação, o terminal normalmente exibirá `(.venv)` antes do caminho atual.

Para desativar o ambiente virtual, execute:

```bash
deactivate
```

Sempre ative o ambiente virtual antes de executar comandos Python relacionados ao projeto.

## 4. Instalar as dependências

Com o ambiente virtual ativado, atualize o pip e instale as dependências do projeto:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

O arquivo `requirements.txt` contém as dependências Python necessárias para executar a aplicação.

## 5. Configurar as variáveis de ambiente

O projeto depende de um arquivo `.env` localizado na raiz do repositório, no mesmo diretório do `manage.py`.

Esse arquivo é necessário para executar a aplicação e deve conter as variáveis de ambiente exigidas pelo projeto, incluindo as credenciais utilizadas pela aplicação e pela autenticação Google.

Crie o arquivo `.env` e configure os valores para seu ambiente. `DATABASE_URL` deve apontar para um banco PostgreSQL existente; por exemplo:

```dotenv
DJANGO_SECRET_KEY=gere-uma-chave-secreta
DEBUG=True
DATABASE_URL=postgresql://usuario:senha@localhost:5432/thesis_manager
```

Mantenha a chave Django e as credenciais do banco apenas no `.env`; esse arquivo é ignorado pelo Git.

Cada desenvolvedor deve configurar seu próprio arquivo `.env`, utilizando as variáveis esperadas pelo `settings.py`.

Para autenticação, configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` e o domínio institucional. Cadastre no Google OAuth a URL de retorno `/accounts/google/login/callback/` no domínio do backend. Em produção, use HTTPS e defina `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True`, `SECURE_SSL_REDIRECT=True`, `DJANGO_ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` e as redes `ADMIN_ALLOWED_CIDRS`. Configure `TRUSTED_PROXY_COUNT` somente quando o backend estiver atrás de proxies confiáveis.

O fluxo da API começa em `GET /api/v1/auth/google/start`; a resposta contém a URL OAuth. A sessão é consultada em `GET /api/v1/session` e encerrada em `DELETE /api/v1/session`. O frontend deve enviar cookies (`credentials: 'include'`) e o cabeçalho `X-CSRFToken` nas requisições de escrita autenticadas. Administradores em rede permitida podem emitir convites em `POST /api/v1/admin/external-invitations`; o token aparece somente nessa resposta e deve ser entregue ao membro externo por canal apropriado. O mesmo recurso lista convites; `DELETE /api/v1/admin/external-invitations/<id>` revoga o convite e invalida a sessão externa.

Não compartilhe credenciais reais nem envie o arquivo `.env` para o repositório. Verifique também se ele está listado no `.gitignore`.

## 6. Preparar as migrações

Antes das migrações, crie um usuário e um banco PostgreSQL para a aplicação. Os comandos abaixo devem ser executados em um terminal com acesso ao PostgreSQL. Eles usam `sudo -u postgres` (comum em Linux); se sua instalação não usar `sudo`, conecte-se como administrador com `psql -U postgres`.

Abra o console administrativo do PostgreSQL:

```bash
sudo -u postgres psql
```

No prompt do `psql`, crie o usuário da aplicação e o banco, substituindo a senha de exemplo por uma senha forte. O nome do banco, do usuário e a senha devem corresponder à URL configurada em `DATABASE_URL`:

```sql
CREATE ROLE thesis_manager_user WITH LOGIN PASSWORD 'substitua-por-uma-senha-forte';
CREATE DATABASE thesis_manager OWNER thesis_manager_user;
```

Saia do console:

```sql
\q
```

Por exemplo, usando os nomes acima, configure `DATABASE_URL` no `.env` como:

```dotenv
DATABASE_URL=postgresql://thesis_manager_user:substitua-por-uma-senha-forte@localhost:5432/thesis_manager
```

Com o ambiente virtual ativado, o PostgreSQL em execução e o `.env` configurado, aplique as migrações:

```bash
python manage.py migrate
```

Esse comando aplica as migrações pendentes do Django.

Após alterações nos modelos, gere as migrações correspondentes com:

```bash
python manage.py makemigrations
```

Em seguida, aplique-as:

```bash
python manage.py migrate
```

## 7. Criar um usuário administrativo

Para criar um usuário administrativo do Django, execute:

```bash
python manage.py createsuperuser
```

Siga as instruções apresentadas no terminal.

## 8. Executar o servidor

Inicie o servidor de desenvolvimento:

```bash
python manage.py runserver
```

Por padrão, a aplicação estará disponível em:

http://127.0.0.1:8000/

Para interromper o servidor, pressione `Ctrl+C`.

O servidor de desenvolvimento deve ser utilizado apenas para desenvolvimento e testes locais.

## 9. Comandos úteis

Verificar a configuração do projeto:

```bash
python manage.py check
```

Executar os testes:

```bash
python manage.py test
```

Consultar os comandos disponíveis:

```bash
python manage.py help
```
