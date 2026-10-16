# Thesis Manager UFVJM Backend

Backend do sistema de gerenciamento de Trabalhos de Conclusão de Curso (TCC), desenvolvido com Django e Django REST Framework.

## Requisitos

Antes de começar, instale os seguintes componentes:

- Python 3
- pip
- venv
- Git

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

Cada desenvolvedor deve configurar seu próprio arquivo `.env`, utilizando as variáveis esperadas pelo `settings.py`.

Não compartilhe credenciais reais nem envie o arquivo `.env` para o repositório. Verifique também se ele está listado no `.gitignore`.

## 6. Preparar as migrações

Com o ambiente virtual ativado e o arquivo `.env` configurado, execute:

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
