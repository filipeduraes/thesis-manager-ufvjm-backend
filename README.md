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

### Decisão de autenticação: Google Workspace institucional

O sistema **não mantém login e senha locais** para usuários institucionais. A
autenticação é feita exclusivamente pelo Google Workspace, por meio do
`django-allauth` em modo headless/social (`SOCIALACCOUNT_ONLY=True`). Além da
autenticação do próprio Google, o backend valida o domínio institucional
(`INSTITUTIONAL_EMAIL_DOMAIN`), o `email_verified` e o `hd` retornados pelo
provedor.

Assim, a credencial (login e senha) é conferida pelo provedor de identidade da
UFVJM e a aplicação recebe apenas uma sessão já autenticada. Não armazenamos
senhas, hashes de senha nem fluxo de recuperação no banco: isso reduz a
superfície de exposição e evita duplicar a política de senhas da instituição.
O primeiro login provê o usuário local e atribui o perfil padrão `STUDENT`; os
demais perfis são derivados do banco, nunca do cliente.

Membros externos da banca não possuem conta permanente: acessam por token
temporário com TTL e escopo limitado, sem cadastro local.

Essa decisão é tratada como política do projeto: o requisito de "login e senha"
do enunciado é atendido por delegação ao IdP institucional, com integração ao
domínio da faculdade. Não introduzir formulário de senha, cadastro local nem
recuperação de senha sem uma decisão explícita e documentada.

## Iniciar o backend com Docker Compose

O comando padrão inicia PostgreSQL, Redis, RabbitMQ, Mailpit, o backend e os workers de eventos. Não requer o repositório do frontend:

```bash
docker compose up --build
```

Ordem de inicialização:

1. `db`, `redis`, `rabbitmq` e `mailpit` sobem e passam nos healthchecks.
2. `init` roda uma única vez `migrate` e `declare_event_topology` (exchange `thesis.events`, fila `notifications.thesis-events` e DLQ) e termina.
3. Só depois que `init` termina com sucesso sobem:
   - `backend`, em `http://localhost:8000`;
   - `event-publisher`, que publica no RabbitMQ os eventos gravados na outbox;
   - `notification-consumer`, que envia as notificações por e-mail. No Compose, todo e-mail vai para o Mailpit (veja "E-mail em desenvolvimento: Mailpit").

Para parar, pressione `Ctrl+C`; os dados permanecem nos volumes Docker. O painel do RabbitMQ fica em `http://localhost:15672`, com o usuário e a senha de `RABBITMQ_DEFAULT_USER` e `RABBITMQ_DEFAULT_PASS`.

Para acompanhar os eventos e as notificações:

```bash
docker compose logs -f event-publisher notification-consumer
```

### E-mail em desenvolvimento: Mailpit

#### O que é e por que usamos

O [Mailpit](https://mailpit.axllent.org/) é uma caixa de entrada falsa para desenvolvimento. No Docker Compose, todo e-mail que o sistema envia (notificações, convites de banca e qualquer outro) é entregue a ele e para por ali: **nenhuma mensagem sai para a internet**, mesmo quando o destinatário é um endereço real como `alguem@gmail.com`. Assim dá para testar o envio sem incomodar ninguém.

O motivo principal é o convite do membro externo da banca. O código de acesso dele (`UFVJM-XXXX-XXXX-XXXX-XXXX`) só existe no e-mail: o banco guarda apenas o hash e a tabela `notification` guarda uma cópia com `UFVJM-****`. A alternativa comum em desenvolvimento, o backend de console do Django (`django.core.mail.backends.console.EmailBackend`), imprimiria o e-mail inteiro, com o código, no log dos containers. Com o Mailpit, o código fica só na caixa de entrada local.

O serviço `mailpit` do `docker-compose.yml` usa a imagem `axllent/mailpit:v1.32.0` e sobe junto com os outros (`docker compose up`); não há nada para instalar.

#### Abrir a caixa de entrada

Com o Compose no ar, abra no navegador:

```text
http://localhost:8025
```

A interface só responde na própria máquina (`127.0.0.1`); outras máquinas da rede não a alcançam. A porta SMTP do Mailpit (1025) não é publicada: só os containers do Compose conseguem entregar e-mails a ele.

#### Encontrar o convite e copiar o token

1. Na lista, procure o e-mail com o assunto **"Convite para banca de TCC: <título da monografia>"**. A coluna "To" mostra o e-mail do membro externo. Para filtrar, digite na busca do topo `to:email-do-membro@exemplo.com` ou `subject:Convite`.
2. Clique no e-mail para abri-lo. O código aparece logo abaixo de "Seu código de acesso é:", no formato `UFVJM-XXXX-XXXX-XXXX-XXXX`.
3. Selecione o código e copie. Use-o em `http://localhost:3000/login`, opção **"Sou membro de banca externo"**, de preferência numa janela anônima para não misturar com a sua sessão.

Cuidados:

- Cada "Reenviar convite" ou "Gerar novo convite" envia um e-mail novo e invalida o código anterior. Use sempre o e-mail **mais recente** daquele destinatário; os mais antigos continuam na caixa, mas os códigos deles não funcionam mais.
- O código funciona uma única vez. Depois de entrar, o convite fica como "utilizado" e é preciso gerar um novo convite.
- Não cole o código em issues, chats ou commits. Ele é uma credencial de acesso, mesmo em desenvolvimento.

#### Apagar os e-mails

- **Um e-mail:** abra a mensagem e clique em **Delete** na barra de cima.
- **Todos:** clique em **Delete all** na barra lateral esquerda.
- **Pelo terminal** (apaga todos):

  ```bash
  curl -X DELETE http://localhost:8025/api/v1/messages
  ```

#### Os e-mails não ficam guardados

O Mailpit guarda as mensagens num banco temporário dentro do próprio container, sem volume Docker. Elas **somem quando o container do Mailpit é parado, reiniciado ou recriado** (`docker compose down`, `docker compose restart mailpit`, `docker compose up --force-recreate`). Além disso, a caixa guarda no máximo 500 mensagens (`MP_MAX_MESSAGES`); acima disso, as mais antigas são apagadas.

Se você precisa de um código que sumiu, não tente recuperá-lo: na tela da banca, use "Reenviar convite" (ou "Gerar novo convite", se o convite já foi usado ou expirou) e um código novo chega em instantes.

#### A caixa está vazia: o que conferir

Siga a ordem abaixo; cada passo elimina uma causa.

1. **O consumidor de notificações está rodando?** É ele quem envia os e-mails.

   ```bash
   docker compose ps notification-consumer event-publisher
   docker compose logs --tail 30 notification-consumer
   ```

   Os dois serviços devem estar `running`. No log, cada envio aparece como `Notification … sent`. Se o consumidor estava parado, os eventos esperam na fila do RabbitMQ: ao religá-lo (`docker compose start notification-consumer`), os e-mails chegam.

2. **O `EMAIL_BACKEND` não está como console?** Dentro do Docker, o `docker-compose.yml` define o `EMAIL_BACKEND` e o SMTP diretamente, então as variáveis `EMAIL_*` do `.env` **não** valem nos containers. Para ver o valor que o consumidor está usando:

   ```bash
   docker compose exec notification-consumer python manage.py shell -c "from django.conf import settings; print(settings.EMAIL_BACKEND, settings.EMAIL_HOST)"
   ```

   O esperado é `django.core.mail.backends.smtp.EmailBackend mailpit`. O `python manage.py check` também avisa (`notifications.W001`) quando o backend de console está configurado.

   Confira também o seu `.env`: se ele tiver `EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend` (o padrão de versões antigas do `.env.example`), troque pela linha atual do `.env.example` (`django.core.mail.backends.smtp.EmailBackend`). Ela só faz diferença quando o backend roda fora do Docker, mas com console o código do convite seria impresso no terminal. Fora do Docker, os e-mails também não chegam ao Mailpit, porque a porta SMTP dele não é publicada; para testar e-mails, rode o consumidor pelo Compose.

3. **O envio falhou?** Procure `failed` no log do consumidor. As notificações que falharam ficam `FAILED` e podem ser reenviadas com:

   ```bash
   docker compose exec -T backend python manage.py consume_notifications --retry-pending
   ```

   Para convites, esse comando gera um código novo em vez de reenviar o antigo.

4. **O Mailpit foi recriado depois do envio?** Nesse caso, a mensagem se perdeu (veja "Os e-mails não ficam guardados"). Reenvie o convite pela tela da banca.

### E-mail em produção

O `EMAIL_BACKEND` e o SMTP institucional (`EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` e `DEFAULT_FROM_EMAIL`) são configurados à parte, nas variáveis do ambiente de implantação. Não use o Mailpit, o backend de console nem os valores do `.env.example` em produção.

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

### Suítes de teste

```bash
# Rápida (~1 min): rode a cada mudança. Regras de domínio, permissões,
# roteamento entre serviços, transições de estado e contratos de API.
DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e python manage.py test_fast

# Completa (~8 min): depois de mudanças grandes, antes de finalizar/mergear.
DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e python manage.py test

# E2E (frontend): inclui os fluxos de UI; ~1 min.
cd ../thesis-manager-ufvjm-frontend && npm run test:e2e
```

As duas suítes usam `settings_e2e` (SQLite, cache em memória, e-mail em memória). O
banco, o cache e o diretório de mídia são derivados do PID do processo, então
**dois agentes podem rodar `test_fast`/`test` ao mesmo tempo sem conflito**.

Para duas suítes E2E simultâneas, use o script que aloca portas livres e nomes
únicos (o `npm run test:e2e` comum usa as portas padrão 8001/3001/8002):

```bash
cd ../thesis-manager-ufvjm-frontend && ./scripts/run-e2e-isolated.sh
```

As variáveis `E2E_RUN_ID`, `E2E_DB_NAME`, `E2E_MEDIA_ROOT`, `E2E_BACKEND_PORT`,
`E2E_FRONTEND_PORT` e `E2E_WORKER_PORT` permitem controlar manualmente o isolamento.

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
python manage.py cleanup_events --days 30 --dry-run            # simula a limpeza de eventos antigos
python manage.py cleanup_events --days 30                      # remove eventos publicados e marcadores antigos
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

Os uploads (PDFs, documentos e texto extraído) ficam no volume nomeado `media_data`, montado em `/app/media`. `docker compose down` preserva o volume; `docker compose down -v` remove banco, cache, fila **e arquivos enviados**. O diretório `./media` do checkout é ignorado pelo Git e não é mais usado pelo Compose.

Cada serviço tem seu próprio banco (`AUTH_DATABASE_URL`, `REPOSITORY_DATABASE_URL`, etc.). O Compose define `SERVICE_DATABASES_SHARED=False`, ativando a separação física. Fora do Compose (desenvolvimento/E2E) o padrão é `True`, com todos os aliases no mesmo SQLite/Postgres.

As leituras entre serviços foram convertidas para **snapshots locais** (`Thesis.advisor_name/course_name/course_code/main_author_name`, `Defense.thesis_snapshot`) e para consultas de tabela única roteadas ao banco dono; não há mais JOINs SQL entre bancos de serviços diferentes. `ServiceDatabaseRouter` está coberto por testes de separação.

**Solução de problemas:** se você subiu o Compose com versões anteriores do roteador, os bancos `thesis_*` podem conter registros em `django_migrations` sem as tabelas correspondentes (o erro `relation "auth_group" does not exist`). Como o ambiente é de desenvolvimento sem dados reais, recrie os volumes:

```bash
sudo docker compose down -v
sudo docker compose --profile frontend up --build
```

## Teste de fumaça: convite de membro externo por e-mail

Roteiro manual com o RabbitMQ real e o Mailpit (os testes automatizados usam um broker em memória):

1. Suba o ambiente com `docker compose --profile frontend up --build` e entre como quem gerencia uma banca agendada (administrador, coordenador do curso ou orientador da tese).
2. Na tela da banca, adicione um membro externo. A tela mostra "Envio do convite em processamento" e, em seguida, "Convite enviado para <e-mail>"; nenhuma resposta da API traz o token.
3. Abra `http://localhost:8025`, leia o e-mail "Convite para banca de TCC" e use o código na tela de login ("Sou membro de banca externo") em uma janela anônima.
4. Em outro membro externo (ainda não usado), clique em "Reenviar convite": o código anterior deixa de funcionar na hora e chega um e-mail novo com outro código.
5. Pare o consumidor (`docker compose stop notification-consumer`), reenvie o convite, confirme que a tela mostra o envio em processamento e religue-o (`docker compose start notification-consumer`): o e-mail chega uma única vez.
6. Procure o token recebido em todos os lugares onde ele não pode aparecer (troque `UFVJM-XXXX-XXXX-XXXX-XXXX` pelo código do e-mail); todos os comandos devem retornar vazio ou `0`:

```bash
TOKEN=UFVJM-XXXX-XXXX-XXXX-XXXX
docker compose logs --no-color | grep -c "$TOKEN"
docker compose exec -T db psql -U thesis_manager_user -d thesis_manager -tAc "SELECT count(*) FROM outbox_event WHERE payload::text LIKE '%$TOKEN%'"
docker compose exec -T db psql -U thesis_manager_user -d thesis_manager -tAc "SELECT count(*) FROM notification WHERE body LIKE '%$TOKEN%' OR body_html LIKE '%$TOKEN%' OR error_message LIKE '%$TOKEN%'"
docker compose exec -T db psql -U thesis_manager_user -d thesis_manager -tAc "SELECT count(*) FROM audit_log WHERE metadata::text LIKE '%$TOKEN%' OR changes::text LIKE '%$TOKEN%'"
```

As mensagens publicadas também podem ser inspecionadas no painel do RabbitMQ (`http://localhost:15672`, fila `notifications.thesis-events`, "Get messages" com o consumidor parado): o payload de `defense.external_member_invited` só tem identificadores e o `trigger`.

