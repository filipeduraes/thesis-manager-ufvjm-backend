# Funcionalidades do Gerenciador de TCCs da UFVJM (status)

Este documento descreve o que está implementado no **backend** deste repositório (`thesis-manager-ufvjm-backend`). O frontend vive em `thesis-manager-ufvjm-frontend`; quando uma integração de tela é citada, ela foi verificada no repositório separado. A referência oficial dos requisitos é [`Requisitos.md`](Requisitos.md), que não foi alterado.

> Atualizado em 2026-11-23, após a auditoria final de encerramento.

Legenda: **Implementado** = código e teste/validação existentes; **Parcial** = há implementação com limitação explícita; **Pendente** = sem implementação funcional; **Não verificável** = faltam condições de evidência.

## 1. Matriz de conformidade com `Requisitos.md`

| Requisito do enunciado | Status | Evidência | Observação |
|---|---|---|---|
| Django, DRF, Python, PostgreSQL | Implementado | `requirements.txt`, `docker-compose.yml`, migrações em 6 aliases | Django 6.1, DRF 3.18, Python 3.12, PostgreSQL 16. |
| Redis (cache, sessão, uploads, rate limit) | Implementado | `CACHES`, `SESSION_ENGINE`, `theses/uploads.py`, `thesismanagerbackend/throttling.py` | Busca `busca:<sha256>`, diretórios, relatórios, limitadores e marcação `upload_em_andamento:<sha256>`. |
| RabbitMQ + processamento assíncrono | Implementado | `events/` (outbox), `notifications/`, `indexing/`, `adjudication/`, read models | Publicação com confirms, DLQ, idempotência e retry manual. |
| Extração de texto de PDF pela fila | Implementado | evento `thesis.text_extraction_requested`, `indexing/consumer.py` | Busca no texto extraído, sem acento, via `Thesis.search_text`. |
| Frontend em JavaScript / React | Implementado (repo separado) | `thesis-manager-ufvjm-frontend`, 25 specs E2E ativas | Consome `/api/v1`. |
| Grupos de até 5 alunos | Implementado | `THESIS_MAX_AUTHORS`, `authors` em `PUT /theses/me`, `/users/suggest` | Seletor de coautores no frontend. |
| Linux | Implementado | Dockerfile, Compose, scripts | Ambiente de desenvolvimento e testes validado em Linux. |
| Autenticação django-allauth | Implementado | `SOCIALACCOUNT_ONLY`, adaptadores em `accounts/adapters.py` | Google Workspace institucional; sem cadastro/senha local (delegação ao IdP). |
| Perfis e permissões | Implementado | `accounts/permissions.py`, `theses/permissions.py`, serviços de domínio | ADMIN > COORDINATOR > ADVISOR > STUDENT; ator externo por convite. |
| Auditoria de ações | Implementado | `AuditLog` com hash chain, `verify_audit_chain`, `SessionAudit` | Append-only; CSV/JSON; chaves de sessão não são gravadas em claro. |
| Dashboard por perfil | Implementado | telas iniciais do frontend + `GET /reports/overview` | Indicadores reais do backend. |
| CRUD de alunos, cursos, professores/coordenação | Implementado | `accounts/`, `academic/`, endpoints admin | Usuários e cursos soft-delete/validações próprias. |
| Submissão e acompanhamento de monografia | Implementado | `/theses/me`, `/theses/me/files`, `/theses/me/submit`, `/theses/<id>` | Status, versões, pareceres e linha do tempo. |
| Upload PDF: formato, tamanho e SHA-256 | Atendido | `theses/services.py::_validate_pdf_upload` | Valida assinatura `%PDF-`, marcador final `%%EOF`, tamanho (padrão 50 MB) e hash; não há sandbox para extração de PDFs maliciosos. |
| Aprovação do orientador antes da banca | Implementado | `SCHEDULABLE_STATUSES` em `theses/models.py`, `defense_scheduling.py` | Nenhuma rota produz `READY_FOR_DEFENSE` sem parecer de aprovação. |
| Agendamento: disponibilidade e conflito | Parcial | `theses/defense_committee.py`, `start_defense`, `sync_defense_status` | Conflito de horário é bloqueado; confirmação é exigida para **iniciar** a banca, não para agendá-la (o enunciado pede validação no agendamento). |
| Membro externo por token temporário | Implementado | `ExternalInvitation`, evento `defense.external_member_invited`, consumidor de convites | Token apenas no e-mail; hash no banco; uso único; escopo à própria banca. |
| Notificação automática da banca | Implementado | planejadores em `notifications/consumer.py` | E-mail para autores, orientador e avaliadores; coordenação não é destinatária. |
| Avaliação individual e resultado final | Implementado | `theses/defense_evaluation.py`, `adjudication/consumer.py`, `_finalize_defense` | Resultado calculado após todas as avaliações não suplentes; suplentes não avaliam. |
| Publicação automática condicionada | Implementado | `publish_if_ready`, `publication_requirements` | Exige aprovação, versão corrigida se houver ressalvas e autorização validada com integridade conferida. |
| Repositório público somente publicadas | Implementado | `theses/repository.py`, `repository_readmodel/` | `PUBLISHED` + `is_public` + não excluída; sem e-mail nos payloads. |
| Busca por título/autor/orientador/curso/ano/palavras-chave | Implementado | `GET /api/v1/public/theses` | Ano = ano de defesa (`defense_year`); filtros e paginação validados. |
| API REST JSON, endpoints públicos e restritos | Implementado | `/api/v1`, serializers DRF | Respostas consistentes; paginação `count/page/pageSize/totalPages/lastPageCount`. |
| Arquitetura de serviços com APIs próprias | Parcial | seis URLconfs, perfil `microservices`, bancos por serviço, gateway nginx | Serviços separados por processo/banco/API, mas mesmos código e imagem; não são deploys independentes. |
| Escalabilidade para milhares de visitantes | Não verificável | cache, índice, paginação e agregações implementados | Sem teste de carga com métricas; não afirmar capacidade comprovada. |
| Dashboard/relatórios | Implementado | `reports/services.py`, `reports/pdf.py`, `reports/csv_export.py`, read model | `overview`, PDF e CSV; agregação no banco, cache de 60 s. |
| Histórico: quem, o quê, quando | Implementado | `AuditLog`, `AuditEvent`, `audit_log` export | Append-only com hash encadeado e filtros por ator/entidade/período. |

## 2. Detalhamento por área

### Autenticação, usuários e administração

- Usuário customizado `accounts.User` (UUID, e-mail único, `name`, `is_staff`, `is_active`); papéis com histórico em `UserProfileRole` e papel efetivo derivado do banco.
- Login institutional Google com validação de domínio, `email_verified` e `hd`; `/api/v1/session`, `/api/v1/auth/google/start`, `DELETE /api/v1/session`.
- Sessão externa por convite (`ExternalInvitationSessionAuthentication`), com expiração, uso único e CSRF nas requisições não seguras.
- `/api/v1/auth/dev-login` e `/api/v1/auth/dev-users` existem **somente** com `DEBUG=True` (padrão do Compose agora é `False`).
- CRUD admin de usuários, cursos e departamentos; diretórios `/api/v1/users`, `/api/v1/users/suggest` e `/api/v1/public/advisors` com escopo e cache próprios.
- Restrição de rede administrativa por `ADMIN_ALLOWED_CIDRS` com proxy confiável configurável.

### Monografias e arquivos

- `theses/models.py`: `Thesis`, `ThesisAuthor`, `StoredFile`, `ThesisFile`, `ThesisReview`, `ThesisStatusHistory`, `Keyword`, `ThesisDocument`.
- Upload somente PDF com assinatura `%PDF-` e marcador final `%%EOF`, limite configurável, nome gerado por UUID, SHA-256 e deduplicação; marcação `upload_em_andamento:<sha256>` no Redis e constraint única de SHA-256 no banco para uploads concorrentes.
- Integridade recalculada antes de submissão, parecer, publicação e download público.
- Histórico de versões, upload da versão corrigida, documentos complementares com validação e checagem de hash do termo de autorização.
- Exclusão lógica protegida para monografias nunca publicadas; publicadas são bloqueadas no modelo, queryset, cascata e admin. Despublicação `PUBLISHED → ARCHIVED` exige motivo; não há republicação.

### Bancas, avaliação e defesa

- Agendamento exige `ADVISOR_APPROVED`/`READY_FOR_DEFENSE`; admin, coordenador do curso ou orientador da própria tese podem agendar.
- Local obrigatório e URL válida quando `modality=REMOTE`; conflito de horário verificado ao adicionar membro e ao reagendar.
- Disponibilidade (`PENDING`/`CONFIRMED`/`DECLINED`) com endpoints do próprio membro; `start_defense` e abertura automática exigem confirmação dos não suplentes.
- Avaliação individual com nota 0–10, parecer e recomendação; edição permitida somente antes do encerramento e apenas pelo próprio avaliador.
- Encerramento manual e automático usam `_finalize_defense`; histórico de status e evento `defense.completed` com `result`/`automatic`. Resultado exposto em `ThesisStatusHistory.metadata` (`defenseId`, `defenseResult`).
- Reversão administrativa de status (`IN_PROGRESS → SCHEDULED`, `CANCELLED → anterior`), com `reason` obrigatório; estados publicados/arquivados não podem ser revertidos nem encerrados.
- Resultado manual em nível de tese (`POST /theses/<id>/defense-result`) permanece apenas para casos sem banca: é recusado quando existe banca ativa ou a monografia já foi publicada/arquivada.

### Repositório e cache

- Busca com `q`, `title`, `author`, `advisor`, `course`, `keyword`, `courseId`, `advisorId`, `year`, `ordering`, paginação e throttle por IP.
- Cache `busca:<sha256>` com versão rotacionada no commit da origem **e** após o commit da projeção, evitando página antiga entre os dois momentos.
- Filtros/cursos/detalhe em cache; download com nova verificação SHA-256; erros sempre em JSON.
- Cadastro de acervo (`/theses/legacy`), edição e troca de PDF de publicadas limitadas a admin/coordenação do curso; gestão centralizada em `theses/repository_management.py`.

### Relatórios e auditoria

- `GET /api/v1/reports/overview`: resumo, `byCourse`, `byAdvisor`, `byDefenseYear`, `options`; filtros `courseId`, `advisorId`, `year`, `scope=coordinated`.
- Admin/superuser vê todos os cursos; coordenador vê apenas seus cursos; staff sem papel de relatório, orientador, aluno e sessão externa recebem 403.
- Taxa de aprovação = (aprovadas + com ressalvas) / bancas com resultado; tempo médio = última submissão antes da banca até `scheduled_datetime`, em dias com uma casa decimal.
- PDF A4 em português e CSV com os mesmos números do payload em cache; cada exportação gera `AuditLog.DOWNLOAD` com identificadores dos filtros.
- `AuditLog` append-only com hash SHA-256 encadeado; `verify_audit_chain` verifica a cadeia; exportações CSV/JSON neutralizam fórmulas de planilha.

## 3. Correções realizadas na auditoria final (2026-11-23)

| Correção | Local | Validação |
|---|---|---|
| Gateway nginx apontava para portas 8000, mas as APIs de serviço escutam em 8011–8016 | `docker/nginx.conf` | teste de configuração `test_microservices_config` |
| Gateway e backend publicavam a mesma porta 8000 no perfil `microservices` | `docker-compose.yml`, `.env.example` | teste de configuração |
| Eventos de catálogo sem planejador (`text_extraction_requested`, `text_extracted`, `repository_updated`) caíam na DLQ do consumidor de notificações | `notifications/consumer.py` | `notifications.tests.test_consumer` |
| Lock de convite externo aberto no alias errado em bancas reenviadas/regeneradas | `theses/defense_committee.py` | `thesismanagerbackend.tests.test_transaction_aliases` |
| Corrida na deduplicação de PDFs entre uploads simultâneos | `theses/services.py` (savepoint + `IntegrityError` reutiliza o arquivo canônico); marcação Redis mantida como antes | `theses.tests.test_upload_tracking` |
| Limite diário de upload não era atômico | `thesismanagerbackend/throttling.py` | teste concorrente em `test_upload_throttle` |
| Cache público podia ficar obsoleto até o TTL após a projeção assíncrona | `theses/repository_cache.py`, `repository_readmodel/projections.py` | `repository_readmodel.tests.test_projections` |
| Resultado manual podia ser lançado com banca ativa ou sobrescrever monografia publicada | `theses/services.py`, `theses/defense_scheduling.py` | `theses.tests.test_coordinator_scope_publication` |
| Coordenador de outro curso podia publicar/validar documento/resultado manual de tese que orientava | `theses/services.py` | teste acima |
| Token de serviço e chave secreta com valores padrão publicamente conhecidos | `settings.py`, `docker-compose.yml`, `.env.example`, `accounts/views.py` | `accounts.tests.test_service_introspection` |
| Staff sem papel de relatório recebia 200 vazio em vez de 403 | `reports/views.py` | `reports.tests.test_overview_api` |
| Chaves de sessão em claro no log de auditoria | `accounts/signals.py` | `accounts.tests.test_audit_quality` |
| Fórmulas de planilha em CSV exportados | `thesismanagerbackend/csv_utils.py`, `reports/csv_export.py`, `accounts/views.py` | `thesismanagerbackend.tests.test_csv_utils` |
| Projetores de read model aceitavam eventos de versão futura | `repository_readmodel/projections.py`, `reports_readmodel/projections.py` | testes de projeção |
| Chaves de cache de diretório com valores não normalizados | `accounts/directory.py` | `accounts.tests.test_directory` |
| Limpeza de eventos misturava transações de bancos diferentes | `events/management/commands/cleanup_events.py` | `events.tests.test_cleanup_events` |
| Logs de apps fora do runserver sem formato/nível | `settings.py` | `thesismanagerbackend.tests.test_logging` |
| `GET /api/v1/public/courses` retornava 500 no serviço de repositório (chamada sem argumento obrigatório) | `repository_readmodel/views.py` | `repository_readmodel.tests.test_projections` + smoke no gateway/serviço |
| PDF com apenas `%PDF-` no início era aceito (polyglot/arquivo truncado) | `theses/services.py` | `theses.tests.test_pdf_validation` — TDD: 2 testes falharam antes da implementação; um terceiro caso (comentário após `%%EOF`, usado no E2E) falhou e levou à checagem do marcador no trecho final |
| Documentação desatualizada, referência a arquivo inexistente e endpoint de depuração sem relação com o domínio | `README.md`, este arquivo, `AGENTS.md`, `thesismanagerbackend/urls.py` | revisão final |

## 4. Limitações e riscos remanescentes

- **Requisitos de negócio:** confirmação de disponibilidade não bloqueia o agendamento, apenas o início da banca (mudança exigiria alterar o fluxo de convites após o agendamento).
- **Arquitetura:** serviços compartilham código e imagem; não há deploy independente nem descoberta de serviços.
- **Mensageria:** não há retry automático para notificações `FAILED`; a adjudicação descarta o evento quando `_finalize_defense` falha transitoriamente (o fechamento manual continua disponível). A emissão de token de convite e a gravação da notificação ocorrem em bancos diferentes, sem transação distribuída.
- **Concorrência:** duas entregas simultâneas do mesmo evento de convite podem emitir tokens em sequência; a invalidação do token anterior e o evento na outbox pertencem a bancos diferentes.
- **Desempenho:** sonda controlada no stack local (200 requisições, 20 concorrentes, busca em cache): 0 erros, p50 ≈ 50 ms, p95 ≈ 89 ms. Isso **não** comprova suporte a milhares de visitantes; não houve teste de carga com dados e concorrência representativos.
- **Infraestrutura:** containers rodam como root; `ADMIN_ALLOWED_CIDRS` no Compose inclui a sub-rede Docker para permitir o gateway, o que é mais amplo que o menor privilégio.
- **Frontend:** o repositório frontend ainda contém observações antigas no próprio README sobre o repositório público e E2E; as especificações estão ativas e não usam `fixme`.

## 5. Testes e validação

| Suíte | Situação |
|---|---|
| `python manage.py test_fast` | 210 testes, OK (41 módulos curados), após a auditoria. |
| `python manage.py test` | 704 testes, 6 falhas conhecidas do SQLite (detalhes no README). |
| `./scripts/test-postgres.sh` | 704 testes, **OK** nos 6 bancos PostgreSQL separados (`SERVICE_DATABASES_SHARED=False`). |
| E2E Playwright | 25 especificações ativas no repositório frontend; `npm run test:e2e`. |

## 6. Endpoints principais

| Método | Rota | Acesso |
|---|---|---|
| `GET` | `/health`, `/api/v1/session` | anônimo / anônimo |
| `GET` | `/api/v1/public/theses`, `/filters`, `/<id>`, `/<id>/download` | anônimo |
| `GET` | `/api/v1/public/courses`, `/public/advisors` | anônimo |
| `PUT`, `POST` | `/api/v1/theses/me`, `/files`, `/submit`, `/withdraw` | aluno autenticado |
| `GET` | `/api/v1/theses`, `/theses/<id>` | conforme escopo |
| `POST` | `/theses/<id>/reviews`, `/documents`, `/documents/<id>/validate`, `/publish`, `/unpublish`, `/defense-result` | orientador/coordenação/admin |
| `POST` | `/theses/<id>/defenses`, `/theses/legacy`, `/theses/<id>/published`, `/published/files` | coordenação/admin |
| `GET`, `POST`, `PATCH` | `/api/v1/defenses`, `/defenses/<id>`, `/theses/<id>/defenses` | institucional, conforme escopo |
| `POST` | `/defenses/<id>/start`, `/close`, `/cancel`, `/revert-status` | gestão da banca / admin |
| `POST`, `PUT`, `DELETE` | `/defenses/<id>/members[...]`, `/members/<id>/evaluation` | membro da banca / gestão |
| `GET`, `POST`, `PATCH`, `DELETE` | `/api/v1/admin/users`, `/courses`, `/departments`, `/audit-logs`, `/external-invitations` | admin restrito por rede |
| `GET` | `/api/v1/reports/overview`, `/overview/pdf`, `/overview/csv` | coordenação/admin |

A lista completa de rotas está em `theses/urls.py`, `accounts/urls.py`, `academic/urls.py` e `reports/urls.py`.
