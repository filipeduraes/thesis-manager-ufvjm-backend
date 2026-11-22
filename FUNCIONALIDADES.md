# Funcionalidades — Thesis Manager UFVJM (status)

Este arquivo resume o que já está implementado e o que ainda falta no sistema, com foco no **backend** deste repositório (`thesis-manager-ufvjm-backend`). O frontend é mantido em repositório separado (`thesis-manager-ufvjm-frontend`); alguns itens abaixo indicam integração frontend já realizada quando relevante.

> Atualizado em: 2026-11-22. Este status é descritivo e pode ficar desatualizado conforme o projeto evolui.

## Legenda
- **Implementado**: funcionalidade disponível no backend (e, quando indicado, integrada no frontend).
- **Parcial**: base/endpoints existem, mas faltam regras de domínio, escopo, UI de fluxo ou integrações.
- **Pendente**: não implementado.

---

## 1. Infraestrutura e base do projeto

| Item | Status | Observações |
|---|---|---|
| Django + DRF + Python | **Implementado** | Projeto `thesismanagerbackend`; apps `accounts`, `academic`, `theses`, `reports`, `events` e `notifications`. |
| PostgreSQL | **Implementado** | Configuração via `DATABASE_URL`; Docker Compose com serviço `db`. |
| Redis | **Parcial** | `REDIS_URL`/cache configurados (`CACHES`, sessão `cached_db`); falta validar/implementar todos os usos previstos (cache de busca/sessão, rastreamento de upload em andamento, rate limiting completo conforme requisitos). |
| Fila de eventos (RabbitMQ/Kafka) | **Implementado** | RabbitMQ com *transactional outbox* (apps `events` e `notifications`, biblioteca `pika`). As transições gravam `outbox_event` na mesma transação; o comando `publish_outbox_events` publica na exchange `topic` `thesis.events` com *publisher confirms*, tentativas e backoff exponencial (`attempts`, `max_attempts`, `next_attempt_at`, `last_error`); o comando `consume_notifications` consome a fila `notifications.thesis-events` (DLQ `notifications.thesis-events.dlq`) de forma idempotente via `processed_event`. Variáveis `RABBITMQ_*`, `EVENTS_EXCHANGE`, `OUTBOX_*`, `NOTIFICATIONS_*` no `.env.example`. Conexões ociosas mantidas por heartbeat (`keep_alive`); queda do broker (inclusive host sem DNS) interrompe o lote sem gastar tentativas e os workers reconectam sozinhos com backoff até 60 s (validado com o RabbitMQ parado e religado). Extração/indexação de texto ainda não usam a fila (comando `extract_thesis_text` sob demanda). |
| Variáveis de ambiente / `.env` | **Implementado** | Segredos/configurações via ambiente; `.env.example` com nomes e valores fictícios (inclui `THESIS_MAX_PDF_SIZE_MB`, `THESIS_MAX_DOCUMENT_SIZE_MB`, `THESIS_MAX_KEYWORDS`). |
| Health check | **Implementado** | `GET /health` (`HealthView`). |
| Docker Compose | **Implementado** | Postgres, Redis, RabbitMQ (`rabbitmq:4.1-management-alpine`, painel só em `127.0.0.1:15672`), Mailpit (`axllent/mailpit:v1.32.0`, caixa de e-mail de desenvolvimento: interface só em `127.0.0.1:8025`, SMTP 1025 sem porta publicada; o compose envia todos os e-mails para ele, nunca para o console), serviço one-shot `init` (`migrate` + `declare_event_topology`), backend e workers `event-publisher`/`notification-consumer` (`restart: unless-stopped`), que só sobem após o `init` concluir; perfil opcional para frontend. O `CMD` do `Dockerfile` ainda executa `migrate` antes do `runserver` (útil ao rodar a imagem sozinha), mas o Compose sobrescreve o comando do serviço `backend` com apenas `runserver`; no Compose, as migrações rodam só no `init`. |
| Migrations versionadas | **Implementado** | `accounts/migrations/0001_initial.py` e `0002_external_invitation_token_delivery.py`, `academic/migrations/0001_initial.py`, `theses/migrations/0001_initial.py` a `0006`, `events/migrations/0001_initial.py`, `notifications/migrations/0001_initial.py`, `0002_seed_templates.py` (templates de e-mail), `0003_seed_external_member_invited_template.py` e `0004_notification_status_cancelled.py`. |

## 2. Autenticação e sessão

| Item | Status | Observações |
|---|---|---|
| Usuário customizado (`accounts.User`) | **Implementado** | UUID, e-mail único, `name`, `AbstractBaseUser + PermissionsMixin`, `USERNAME_FIELD=email`. |
| Papéis de negócio com histórico | **Implementado** | `UserProfileRole` (`ADMIN`, `COORDINATOR`, `ADVISOR`, `STUDENT`), revogação sem apagar histórico, restrição de um papel ativo por (usuário, papel). Papel efetivo derivado do banco por prioridade. |
| Múltiplos papéis (exceto Aluno) | **Implementado** | Backend e UI admin permitem combinar perfis institucionais (ex.: Orientador+Coordenador+Administrador); `STUDENT` não pode ser combinado com outros. |
| Login institucional Google Workspace | **Implementado** | `django-allauth` (headless/social), validação de domínio `@ufvjm.edu.br`, `email_verified`/`hd`, provisionamento no primeiro login, papel padrão `STUDENT`. Sem cadastro/senha local. |
| Início do OAuth | **Implementado** | `GET /api/v1/auth/google/start?next=` retorna URL de autenticação. |
| Sessão versionada `/api/v1` | **Implementado** | `GET /api/v1/session` retorna estado da sessão, usuário, papéis, CSRF e contexto de convite externo; `DELETE /api/v1/session` encerra sessão. |
| Sessão em `cached_db` + cookies/CSRF/CORS | **Implementado** | Cookies HttpOnly; CSRF no frontend; CORS/CSRF confiáveis via ambiente. |
| Avatar Google na sessão/admin | **Implementado** | `avatarUrl` (URL `https`) quando disponível na conta social Google. |

## 3. Administração de usuários

| Item | Status | Observações |
|---|---|---|
| Listagem administrativa paginada | **Implementado** | `GET /api/v1/admin/users` (admin, IP/VPN), paginação com metadados (`count,page,pageSize,totalPages,lastPageCount,next,previous,results`), busca por nome/e-mail, filtros por papel/status. |
| Criação administrativa | **Implementado** | `POST /api/v1/admin/users`; valida e-mail institucional e papéis. Nome/e-mail institucionais tratados como fonte confiável (sincronização pelo login). |
| Edição administrativa restrita | **Implementado** | `PATCH /api/v1/admin/users/<id>` altera papéis, status (`is_active`) e `courseId`; nome/e-mail não editáveis. Auditoria consolidada com antes/depois em atualizações relevantes. |
| Desativação (soft delete) | **Implementado** | `DELETE /api/v1/admin/users/<id>` desativa usuário e revoga papéis ativos; protege último administrador e impede auto-desativação administrativa. |
| Associação usuário–curso | **Implementado** | `courseId` no admin de usuários; vínculo ativo único por usuário (`academic_user_course_assignment`). |
| Resolução de coordenador por e-mail | **Implementado** | Ao salvar curso/ao provisionar/logar usuário com e-mail correspondente, coordenação pendente pode ser resolvida e papel `COORDINATOR` garantido (conforme serviços `academic`/`accounts`). |
| Lookup de usuário por e-mail | **Implementado** | `GET /api/v1/admin/users/lookup?email=` para apoio à UI (ex.: coordenador de curso). |
| UI admin de usuários (frontend) | **Implementado** | Lista paginada, busca/filtros automáticos, papéis em checkbox (regra do Aluno), curso, avatar/iniciais, edição sem nome/e-mail, desativação. |

## 4. Convites externos e acesso por token (membro de banca)

| Item | Status | Observações |
|---|---|---|
| Modelo de convite externo | **Implementado** | `ExternalInvitation`: dados do convidado, `token_hash` SHA-256, status, expiração, uso/revogação, `token_event_id` (único evento autorizado a emitir o token) e `token_sent_at` (envio do e-mail); sem armazenar token legível. |
| Geração de token temporário | **Implementado** | Token humano `UFVJM-XXXX-...`, gerado pelo consumidor de notificações e enviado só por e-mail (ver "Envio do token por e-mail"); nenhuma resposta da API devolve o token. TTL configurável (`EXTERNAL_INVITATION_TOKEN_TTL_HOURS`). Uso único: a troca por sessão marca o convite como `USED`. |
| Restrição de cargos para criar convite | **Implementado** | Convites só nascem pela inclusão de membro externo na banca, por quem gerencia a banca (admin, coordenador do curso, orientador da tese). A criação avulsa `POST /api/v1/admin/external-invitations` foi removida (405), junto com `CanInviteExternalCommitteeMember`. |
| Listar/revogar convites | **Implementado** | `GET /api/v1/admin/external-invitations` (admin; inclui `sentAt`, nunca o token), `DELETE /api/v1/admin/external-invitations/<id>` (admin). |
| Troca de token por sessão externa | **Implementado** | `POST /api/v1/auth/external-token`; valida hash/estado/expiração, `flush` da sessão, TTL da sessão externa, auditoria `TOKEN_ACCESS`. |
| Autenticação de sessão externa | **Implementado** | `ExternalInvitationSessionAuthentication` + `ExternalInvitationPrincipal`; CSRF em métodos não seguros; bloqueio por expiração/revogação. |
| Login por token no frontend | **Implementado** | `LoginView` envia token; sessão externa direciona para fluxo de avaliação externa. |
| Convite no frontend | **Implementado** | Branch `feat/external-invite-email` do frontend: a tela da banca e o agendamento mostram "Convite enviado para <e-mail>" (ou envio em processamento), com "Reenviar convite" e "Gerar novo convite"; nenhuma tela exibe o token. A criação avulsa saiu de `AdminUsers`. |
| Escopo do convite à banca/defesa/membro | **Implementado** | Cada `DefenseMember` externo aponta para o seu `ExternalInvitation` (one-to-one). `POST /api/v1/defenses/<id>/members/external` cria o convite e o membro (201, só `member`, sem token). `POST /api/v1/defenses/<id>/members/<memberId>/regenerate-token` ("Gerar novo convite"; mesma permissão de gerenciar a banca; banca `SCHEDULED`/`IN_PROGRESS`) revoga o convite atual, cria outro com validade nova para o mesmo membro (mantém disponibilidade e avaliação; serve para convites usados, expirados ou revogados) e responde 202. O payload do membro traz, só para quem gerencia a banca, `invitationStatus`/`invitationStatusLabel` (expirado é informado mesmo antes de alguém tentar o token), `invitationExpiresAt`, `invitationSentAt` e `canResendInvitation`. `GET /api/v1/session` e `POST /api/v1/auth/external-token` incluem `externalInvitation.assignments` (`[{defenseId, memberId}]`). A sessão externa só lê o detalhe da própria banca, confirma disponibilidade e avalia; outras bancas e ações de gestão retornam 403. |
| Envio do token por e-mail | **Implementado** | Evento `defense.external_member_invited` (payload só com ids e `trigger` `CREATED`/`RESENT`/`REGENERATED`) gravado na mesma transação da inclusão, do reenvio e do novo convite; o token anterior deixa de valer na hora. O consumidor gera o token, grava só o hash, envia o e-mail (template `EXTERNAL_MEMBER_INVITED`: papel, data/hora da banca, código, validade e instruções; sem local/link) e guarda em `notification` só a cópia mascarada (`UFVJM-****`). Reentrega ou evento substituído não geram token nem e-mail. `--retry-pending` gera token novo (nunca reenvia o corpo salvo) e cancela (`CANCELLED`) envios de convites substituídos ou inválidos. Auditoria `INVITE` do envio/reenvio (ator Sistema) e do pedido de reenvio (ator), só com ids. Testes garantem que o token não aparece na outbox, no envelope publicado, em `notification`, no `audit_log` nem nos logs. |
| Reenvio do convite | **Implementado** | `POST /api/v1/defenses/<id>/members/<memberId>/resend-invitation` (202): quem gerencia a banca; banca `SCHEDULED`/`IN_PROGRESS`; convite `PENDING`/`ACCEPTED`, não expirado nem revogado (revogado, usado ou expirado → 400 orientando a gerar novo convite). Não renova a validade. Throttle `invitation_resend` (5/min por usuário, compartilhado com "Gerar novo convite"). |

## 5. Cursos e departamentos (cadastro acadêmico)

| Item | Status | Observações |
|---|---|---|
| Departamentos | **Implementado** | CRUD administrativo: `GET/POST/PATCH/DELETE /api/v1/admin/departments`. |
| Cursos | **Implementado** | CRUD administrativo paginado: `GET/POST/PATCH/DELETE /api/v1/admin/courses`, busca e filtro por departamento. |
| Coordenador por e-mail | **Implementado** | Curso armazena `coordinator` (FK opcional) e `coordinator_email` normalizado; status pendente/ativo/inativo; lookup; frontend normaliza parte local para `@ufvjm.edu.br`. |
| Contagens no curso | **Parcial** | `activeStudents` calculado por vínculos ativos; `publishedTheses` retorna 0 até existir domínio de TCC publicado. |
| Limpeza de departamentos órfãos | **Implementado** | Departamentos sem cursos associados são removidos/evitados conforme regras do serviço. |
| UI admin de cursos/departamentos (frontend) | **Implementado** | CRUD, filtro/busca automáticos, seleção/criação de departamento, indicação de coordenação pendente. |

## 6. Auditoria e segurança operacional

| Item | Status | Observações |
|---|---|---|
| Auditoria append-only com hash chain | **Implementado** | `AuditLog` com SHA-256 encadeado, proteção contra update/delete em nível de modelo/queryset. |
| Auditoria de sessão | **Implementado** | `SessionAudit` com IP, user agent, origem de rede, login/logout/atividade. |
| Endpoints de auditoria admin | **Implementado** | `GET /api/v1/admin/audit-logs` (filtros/paginação) e `GET /api/v1/admin/audit-logs?export=csv|json` / `/api/v1/admin/audit-logs/export`. |
| Resumos descritivos e antes/depois | **Implementado** | Serializer gera resumos legíveis; atualizações de usuário podem registrar transições (ex.: perfil de X para Y, curso/status); UI mostra detalhes, canal e origem da rede/IP. |
| Restrição IP/VPN para áreas admin | **Implementado** | Middleware/CIDRs confiáveis para `/admin/` e `/api/v1/admin/`; nega sem vazar detalhes. |
| Rate limiting | **Parcial** | Throttles DRF presentes em views (ex.: sessão, token externo); falta consolidar política completa (incl. Redis/rate limiting global de uploads conforme requisito). |
| Proteção de segredos e privacidade em logs | **Implementado (diretriz)** | Tokens/senhas não devem ser logados; auditoria evita payloads sensíveis. Revisão contínua recomendada ao adicionar novos fluxos. |

## 7. Domínio de TCC (núcleo do produto)

| Item | Status | Observações |
|---|---|---|
| Modelos de monografia/TCC | **Implementado** | App `theses`: `Thesis`, `ThesisAuthor`, `StoredFile`, `ThesisFile` (versões), `ThesisReview`, `ThesisStatusHistory`, `Keyword`/`ThesisKeyword`, `ThesisDocument`. Status conforme `MODELO_BANCO_DADOS.puml`. |
| Rascunho, submissão e acompanhamento | **Implementado** | `GET/PUT /api/v1/theses/me` cria/atualiza rascunho com metadados, curso, orientador e palavras-chave; `POST /api/v1/theses/me/submit` envia ao orientador; `GET /api/v1/theses/<id>` retorna versões, pareceres, documentos e linha do tempo. |
| Upload de PDF com validação | **Implementado** | `POST /api/v1/theses/me/files` aceita somente PDF (magic bytes `%PDF-`), limite configurável (`THESIS_MAX_PDF_SIZE_MB`, padrão 50 MB), nome de armazenamento gerado, sem confiar em filename/MIME do cliente. |
| SHA-256 do arquivo | **Implementado** | Hash calculado no upload para integridade/deduplicação; rechecagem antes de submissão, parecer e publicação. Metadados expostos sem caminho interno de armazenamento. |
| CRUD de monografias com permissões/estado | **Implementado** | `GET /api/v1/theses` (escopos `advised`, `authored`, `coordinated`, filtros e paginação), `GET /api/v1/theses/<id>`, `POST /api/v1/theses/<id>/reviews` (orientador), `POST /api/v1/theses/<id>/defense-result` e `POST /api/v1/theses/<id>/publish` (coordenação/admin). Permissões e estados validados no backend. |
| Busca/filtros de TCC | **Implementado** | Busca por título, resumo, referência, autor, orientador e palavra-chave; filtros por status, curso, orientador, ano e `pendingReview`; paginação com metadados. |
| Histórico de versões | **Implementado** | Versões mantidas durante a orientação; `purge_thesis_versions` remove versões antigas após aprovação final da banca e na publicação, mantendo apenas o arquivo atual. |
| Documentos complementares | **Implementado** | Upload e validação de ata, autorização de publicação, comprovante de revisão ortográfica e outros documentos; publicação exige autorização validada. |
| UI de submissão/acompanhamento (frontend) | **Implementado** | `StudentSubmission` integra metadados, upload, envio, acompanhamento, linha do tempo, pareceres, versões, leitor de PDF e documentos. Dashboard do aluno usa dados reais. |
| UI de orientação/aprovação (frontend) | **Implementado** | `AdvisorApproval` lista orientandos com filtros/paginação, abre detalhe, lê o PDF com zoom/download, aprova, solicita ajustes, reprova, registra resultado da banca, valida documentos e publica. |

## 8. Banca, avaliação e defesa

| Item | Status | Observações |
|---|---|---|
| Modelos de defesa/banca/membro/avaliação | **Implementado** | `Defense`, `DefenseMember`, `DefenseMemberEvaluation` com `DefenseStatus`, `CommitteeRole`, `DefenseResult`, `DefenseModality`; migration 0002 + 0003 (`duration_minutes`); `DefenseStatusHistory` na migration 0005. |
| Agendamento após aprovação do orientador | **Implementado** | `POST /api/v1/theses/<id>/defenses` exige `ADVISOR_APPROVED` ou `READY_FOR_DEFENSE`; admin, coordenador do curso da tese e orientador da própria tese podem agendar; transições de `ThesisStatus` e histórico registrados. Nota: orientador agenda diretamente (sem "solicitação") — pendente decisão do Filipe. |
| Defesa remota com link | **Implementado** | Com `modality=REMOTE`, `location` é obrigatório e precisa ser URL `http`/`https` (`validate_defense_location`; 400 com `errors.location`). Com `IN_PERSON`, `location` continua texto livre. Validado no agendamento e, no `PATCH`, só quando `modality` ou `location` são enviados e mudam de valor, para que bancas antigas com local fora do padrão continuem editáveis nos demais campos. |
| Disponibilidade e conflitos de horário | **Implementado** | Verificação de sobreposição de horário por membro institucional (intervalo semiaberto: `A.start < B.end AND B.start < A.end`; sessões adjacentes não conflitam) ao adicionar membro à banca e também no `PATCH /api/v1/defenses/<id>` quando `scheduledDatetime` ou `durationMinutes` mudam (`check_committee_schedule_conflicts`, mesma função `_has_schedule_overlap`). Em conflito, retorna 400 com o nome do membro (`"O membro <nome> já participa de outra banca no mesmo período."`). Edições só de local/modalidade/observações não verificam conflito. Membros externos não são verificados (não há identidade entre bancas). Membro pode fornecer `conflictReason` para registrar conflito de interesse. |
| Avaliação individual por membro | **Implementado** | `PUT /api/v1/defenses/<id>/members/<memberId>/evaluation` — cada membro (institucional ou externo via convite válido) registra decisão, nota (0–10) e notas; suplentes não podem avaliar (403); membro não vê nota/notas de outro membro. |
| Encerramento automático com cálculo de resultado | **Implementado** | `POST /api/v1/defenses/<id>/close` (admin/coordenador/orientador da própria tese) verifica quórum, calcula resultado por maioria, transita `ThesisStatus` e chama `purge_thesis_versions` para aprovados. **Mudança de comportamento:** a avaliação registrada (`PUT .../evaluation`, via `record_committee_evaluation`) verifica se todos os não suplentes já avaliaram e, nesse caso, encerra a banca automaticamente pela mesma lógica (`_finalize_defense`, compartilhada com o `close_defense`), num savepoint separado. O histórico grava `changed_by` nulo e motivo "Resultado calculado automaticamente após a última avaliação."; a aprovação dispara `purge_thesis_versions`, como no encerramento manual. Se o encerramento automático levantar `ValidationError` (ex.: aprovação sem PDF atual), a avaliação continua salva, a banca fica `IN_PROGRESS`, é gravado `logger.warning` e a resposta traz `autoCloseError`; o encerramento manual continua disponível. Depois de encerrada, nenhuma avaliação pode ser alterada (400). |
| Registro do resultado final (manual) | **Implementado** | `POST /api/v1/theses/<id>/defense-result` (coordenação/admin) ainda disponível para lançamento manual retroativo. |
| Confirmação de disponibilidade dos membros | **Implementado** | `DefenseMember.availability_status` (`PENDING`/`CONFIRMED`/`DECLINED`), `availability_responded_at`, `availability_note` (migration `0006_defense_member_availability`: membros de bancas `IN_PROGRESS`/`COMPLETED` já existentes ficam `CONFIRMED`; demais, `PENDING`). `POST /api/v1/defenses/<id>/members/<memberId>/availability` com `{"status": "CONFIRMED"|"DECLINED", "note"}`: só o próprio membro (institucional ou sessão externa do seu convite), só com a banca `SCHEDULED`; outro membro/gestor recebe 403; registra `AVAILABILITY_RESPONDED` no AuditLog (sem copiar a nota). O `PATCH` volta todos para `PENDING` apenas quando data/horário ou duração mudam de valor. `start_defense` exige todos os não suplentes `CONFIRMED` (400 com os nomes que faltam); banca sem não suplentes mantém o comportamento anterior. Payload de membro ganha `availabilityStatus`, `availabilityStatusLabel`, `availabilityRespondedAt`, `availabilityNote`; o detalhe ganha `allMembersConfirmed` (há não suplentes e todos confirmaram) e `pendingConfirmations` (`memberId`, `name`, `role`, `roleLabel`, `availabilityStatus`). |
| Abertura automática para avaliação | **Implementado** | `sync_defense_status` transita `SCHEDULED → IN_PROGRESS` quando `scheduled_datetime` já passou, sem job assíncrono. Chamada no início dos endpoints de detalhe, membros e avaliação. Idempotente (audit event `AUTO_STARTED_BY_SCHEDULE`; `AuditAction.UPDATE`). Teses transitam para `UNDER_EVALUATION` pelo mesmo código-caminho do `start_defense` manual. **Mudança de regra (pedida pelo Filipe):** a abertura automática agora exige, como o início manual, que todos os membros não suplentes tenham confirmado disponibilidade; se faltar confirmação no horário, a banca continua `SCHEDULED` sem erro na leitura e o detalhe lista quem falta em `pendingConfirmations`. Banca sem membros não suplentes mantém o comportamento anterior. |
| Reversão de status pelo administrador | **Implementado** | `POST /api/v1/defenses/<id>/revert-status` (somente admin). Permite `IN_PROGRESS → SCHEDULED` e `CANCELLED → status anterior` (campo `previous_status` em `Defense`; fallback para `SCHEDULED`). `COMPLETED` não reverte (400). Campo `auto_start_suspended` impede reabertura automática até que o horário seja reagendado via `PATCH` ou a banca seja iniciada manualmente. `reason` obrigatório (400 se vazio). Audit event `STATUS_REVERTED_BY_ADMIN`. Migration `0004_add_defense_revert_fields`. |
| Iniciar/encerrar banca pelo orientador | **Implementado** | `start_defense` e `close_defense` usam `_can_manage_defense`: admin, coordenador e orientador da própria tese (decisão da revisão: manter o controle da banca com o orientador). Orientador de outra tese que integra a banca recebe 403; usuário que não enxerga a banca recebe 404 (mesmo escopo do detalhe); aluno recebe 403. `revert-status` continua somente admin. |
| Histórico de status da banca | **Implementado** | Modelo `DefenseStatusHistory` (tabela `defense_status_history`, migration `0005_defense_status_history`): `previous_status`, `new_status`, `changed_by` (nulo em transições automáticas), `reason`, `created_at`. Gravado por um único helper (`record_defense_status_change`) dentro da mesma transação de cada transição: agendamento (vazio → `SCHEDULED`), início manual, abertura automática (`changed_by` nulo), encerramento (com resultado), cancelamento (com motivo) e reversão do admin (com motivo). Exposto como `statusHistory` (ordem cronológica) somente em `GET /api/v1/defenses/<id>`, não na listagem. Bancas criadas antes da migration não têm histórico retroativo (sem backfill). |
| Escopo do coordenador nas bancas | **Implementado** | `_can_manage_defense`, `defenses_visible_to`, `schedulable_theses_for` e o `GET/POST /api/v1/theses/<id>/defenses` limitam o coordenador às teses dos cursos que coordena, reaproveitando `theses_visible_to` de `theses/services.py` (vínculo por `course.coordinator`, `coordinator_email` ou `UserCourseAssignment` ativo) via `coordinator_managed_theses`; teses de autoria do próprio coordenador ficam fora. Coordenador de outro curso recebe lista vazia e 404 nas ações (403 se for membro da banca, pois a enxerga mas não a gerencia). Admin continua com acesso total; orientador continua gerenciando só a própria tese e vendo as bancas que integra. |
| Listagem de bancas visíveis ao usuário | **Implementado** | `GET /api/v1/defenses` (paginado). Filtros no padrão de `GET /api/v1/theses`: `?q=` busca sem diferenciar maiúsculas em título da tese, código de referência, nome do aluno principal (coautores não), nome do orientador e local; `?status=` com códigos `DefenseStatus` separados por vírgula (códigos inválidos são ignorados); `?pendingEvaluation=true` (`1`/`yes`) mantém só bancas `SCHEDULED`/`IN_PROGRESS` em que o usuário é membro institucional não suplente sem avaliação enviada. Filtros combináveis; resultado com `distinct()`. Escopo em `theses/defense_listing.py::defenses_visible_to` (mesma regra do detalhe): admin vê todas; coordenador vê as das teses dos cursos que coordena; orientador vê as das teses que orienta e as que integra; aluno vê a da própria tese; membro institucional vê as que integra. Sessão de convite externo recebe 403. Cada item = `defense_payload` + `thesisTitle`, `thesisReferenceCode`, `student`, `advisor`, `viewerMembership` (`isMember`, `memberId`, `role`, `roleLabel`, `canEvaluate`, `hasEvaluated`). Bancas agendadas com horário vencido são iniciadas (`sync_defense_status`) antes do filtro. |
| Teses aptas a agendar banca | **Implementado** | `GET /api/v1/defenses/schedulable-theses` (paginado): status `ADVISOR_APPROVED`/`READY_FOR_DEFENSE`, sem banca `SCHEDULED`/`IN_PROGRESS`, regra de `_can_manage_defense` (admin todas; coordenador só as dos cursos que coordena; orientador só as próprias; demais perfis recebem lista vazia; convite externo 403). Itens no formato de `GET /api/v1/theses`. |
| Resultado e vínculo do usuário no payload da banca | **Implementado** | `defense_payload` (detalhe, listagem e demais respostas de banca) inclui `result` e `resultLabel`: `null` até a banca ficar `COMPLETED`; depois, o resultado calculado no encerramento manual ou automático, lido do registro gravado por `_finalize_defense` em `ThesisStatusHistory.metadata` (`defenseId` + `defenseResult`; prefetch na listagem, sem N+1). O resultado manual da tese (`POST /api/v1/theses/<id>/defense-result`) não tem `defenseId` e não é atribuído a nenhuma banca. O detalhe `GET /api/v1/defenses/<id>` passa a ter `viewerMembership` no mesmo formato da listagem (`isMember`, `memberId`, `role`, `roleLabel`, `canEvaluate`, `hasEvaluated`), para usuário institucional e sessão externa (`viewer_membership_payload`). |
| Foto dos membros da banca | **Implementado** | `defense_member_payload` inclui `avatarUrl`: foto da conta Google (`socialaccount`) do membro institucional, pelas mesmas regras de `_avatar_url` (só URL `https`), via `user_avatar_url` em `accounts/services.py`; `null` para membros externos e para quem não tem conta Google ou foto. Detalhe e listagem fazem prefetch das contas Google (`google_social_accounts_prefetch`) e calculam uma vez por banca se o leitor é admin, então o número de consultas não cresce com o número de membros. `GET /api/v1/admin/users/lookup` também devolve `user.avatarUrl` (campo novo). |
| Permissões do usuário no payload da banca | **Implementado** | Detalhe (`GET /api/v1/defenses/<id>`) e listagem (`GET /api/v1/defenses`) incluem `viewerPermissions` com `canManage`, `canEdit`, `canCancel`, `canStart`, `canClose`, `canRevert` e `canManageMembers`. `canManage` usa a mesma regra de `_can_manage_defense` (que agora delega a `build_defense_viewer_context`); `canEdit`, `canCancel` e `canStart` exigem `canManage` e banca `SCHEDULED`; `canClose`, `canManage` e `IN_PROGRESS`; `canManageMembers`, `canManage` e `SCHEDULED`/`IN_PROGRESS` (remoção de membro continua só em `SCHEDULED`); `canRevert`, admin e `IN_PROGRESS`/`CANCELLED`. Pré-condições que não dependem do usuário não entram nas flags: iniciar também exige `allMembersConfirmed` e encerrar exige todas as avaliações. Sessão de convite externo recebe tudo `false`. Na listagem, os direitos são resolvidos uma vez para a página inteira (consultas fixas, sem consulta extra por banca). |
| Privacidade da disponibilidade da banca | **Implementado** | Em todas as respostas que trazem membros (`defense_payload`/`defense_member_payload`: detalhe, listagem, `GET /api/v1/theses/<id>/defenses` e ações), `availabilityStatus`, `availabilityStatusLabel`, `availabilityRespondedAt` e `availabilityNote` só vêm preenchidos para quem tem `canManage` (admin, coordenador do curso, orientador da tese) e, para os demais, apenas no próprio membro (o mesmo de `viewerMembership`); nos outros membros vêm `null`. `pendingConfirmations` no detalhe só é enviado a quem tem `canManage` (demais recebem `null`); `allMembersConfirmed` continua para todos. Sessão externa vê só a própria disponibilidade. Coordenador de outro curso que integra a banca é tratado como membro comum. |
| Eventos de banca/avaliação | **Implementado** | Rodando no Docker Compose (ver "Fila de eventos"); teste de fumaça com RabbitMQ real em 2026-11-21 (submissão, agendamento, avaliação e encerramento automático, inclusive com o broker parado). Cada transição grava um `OutboxEvent` (versão `1.0`, payload só com identificadores e códigos, chaves fixas em `events/catalog.py`) na mesma transação, por helpers de `theses/events.py`: `thesis.submitted` (`submit_thesis`), `defense.scheduled` (`schedule_defense`), `defense.evaluation_recorded` (`record_committee_evaluation`, sem decisão/nota/parecer), `defense.completed` (`_finalize_defense`, encerramento manual e automático, com `result` e `automatic`; desfeito junto se o auto-close falhar), `thesis.defense_result_recorded` (`record_defense_result`, resultado manual sem banca) e `thesis.published` (`publish_thesis`). O cálculo do resultado continua síncrono. O consumidor envia e-mail pelo `EMAIL_BACKEND` com os templates de `notification_template` (migration `notifications/0002_seed_templates`): submissão → orientador; banca agendada → autores e orientador (sem local/link); avaliação registrada → orientador (só o progresso "X de Y"); banca concluída → autores, orientador e membros avaliadores (suplentes não avaliam nem recebem); resultado manual e publicação → autores e orientador. Coordenador não recebe. Reentrega é detectada por `processed_event` (sem e-mail duplicado); falhas de envio ficam `FAILED` em `notification` e são reenviadas manualmente com `consume_notifications --retry-pending`. Eventos `FAILED` na outbox voltam com `publish_outbox_events --requeue-failed <id|all>`. |

## 9. Repositório público e publicação

| Item | Status | Observações |
|---|---|---|
| Repositório público de TCCs publicados | **Implementado** | `GET /api/v1/public/theses` (busca por título/resumo/autor/orientador/palavra-chave e filtros de curso/orientador/ano) e `GET /api/v1/public/theses/<id>` com payload público; acesso anônimo somente a publicados. |
| Publicação condicionada | **Implementado** | `POST /api/v1/theses/<id>/publish` exige aprovação final, autorização de publicação validada e arquivo atual íntegro; remove versões antigas e publica apenas a versão atual. |
| Proteção contra exclusão de publicado | **Implementado** | Não há endpoint de exclusão de monografia; publicação/arquivamento são transições controladas e o histórico/auditoria é preservado. |
| Recursos públicos (TCCs, cursos, orientadores) | **Parcial** | TCCs publicados, download e orientadores (`GET /api/v1/public/advisors`) estão disponíveis publicamente; falta o endpoint público dedicado de cursos para os filtros do repositório. |

## 10. Relatórios e dashboards

| Item | Status | Observações |
|---|---|---|
| Relatórios administrativos | **Implementado** | `GET /api/v1/reports/overview` (app `reports`): resumo, `byCourse`, `byAdvisor`, `byDefenseYear` e `options`, com filtros `courseId`, `advisorId`, `year` (ano da banca, America/Sao_Paulo) e `scope=coordinated`. Admin vê todos os cursos; coordenador vê só os cursos vinculados (FK `coordinator`, `coordinator_email` ou `UserCourseAssignment` ativo); orientador/aluno/externo recebem 403. Agregação no banco (`Count`/`Avg` com `GROUP BY`), cache Redis de 60 s (`reports:overview:*`) e throttle `reports` (60/min). Taxa de aprovação **por banca concluída**: (aprovadas + com ressalvas) / bancas com resultado. Tempo médio: da última submissão (`thesis_status_history` `SUBMITTED`) anterior à banca até `scheduled_datetime`. O resultado vem de `thesis_status_history.metadata` (`defenseId`/`defenseResult`, gravados por `_finalize_defense`); `Defense` não tem coluna `result` (diferente do `.puml`). |
| Exportação de relatórios em PDF | **Implementado** | `GET /api/v1/reports/overview/pdf` (app `reports`, `reports/pdf.py`, biblioteca ReportLab). Mesmos filtros, permissões, escopo e payload em cache de `reports/overview`; o PDF só formata os números calculados pelo backend. Conteúdo em português: título, data/hora da geração (America/Sao_Paulo), escopo e filtros por nome, resumo (com "Taxa de aprovação em bancas") e tabelas por curso, orientador e ano de defesa. Arquivo `relatorio-tcc-AAAAMMDD-HHMM.pdf` (`attachment`, `no-store`); erros sempre em JSON. Throttle próprio `reports_export` (10/min). Cada exportação gera `audit_log` `DOWNLOAD` (`ReportOverview`) só com identificadores dos filtros. Exportação CSV e histórico em `report_execution` continuam pendentes. |
| Dashboards frontend | **Implementado** | Telas iniciais de aluno, orientador, coordenador e administrador usam dados reais; coordenação e administração consomem `reports/overview`. |

## 11. Frontend (repositório separado, resumo de integração)

| Item | Status | Observações |
|---|---|---|
| Login Google + sessão | **Implementado** | Integração com `/api/v1/session` e início OAuth. |
| Login por token externo | **Implementado** | Troca de token e rota para avaliação externa. |
| Área admin: usuários/cursos/auditoria | **Implementado** | Integrada aos endpoints administrativos; multi-papéis e navegação combinada. |
| Convite de membro externo | **Implementado** | Tela de bancas e agendamento: convite vinculado ao membro, enviado por e-mail, com status, "Reenviar convite" e "Gerar novo convite"; o token não aparece em nenhuma tela. |
| Fluxos de TCC/orientação | **Implementado** | Submissão do aluno, acompanhamento, upload de PDF/versões/documentos, parecer do orientador (aprovar/ajustes/reprovar), leitor de PDF com zoom/download, resultado da banca e publicação. |
| Fluxos de banca/avaliação | **Implementado** | Agendamento (`DefenseScheduling`), gestão da banca (`DefenseCommittee`), avaliação individual (`DefenseEvaluation`) e avaliação externa (`ExternalEvaluation`) consomem os endpoints de `/api/v1/defenses`. |
| Relatórios e dashboards | **Implementado** | `ReportsView` e os dashboards de coordenação/administração consomem `GET /api/v1/reports/overview`; o botão "Exportar PDF" baixa `GET /api/v1/reports/overview/pdf` com os filtros atuais. |
| Repositório público | **Parcial** | O backend possui os endpoints públicos, mas a tela do frontend (`PublicRepository`) ainda usa dados mock. |

## 12. Endpoints de TCC implementados

| Método | Rota | Acesso | Observação |
|---|---|---|---|
| `GET` | `/api/v1/theses/me` | Aluno | Contexto da monografia, opções de curso/orientador e limites. |
| `PUT` | `/api/v1/theses/me` | Aluno | Cria/atualiza rascunho e metadados. |
| `POST` | `/api/v1/theses/me/files` | Aluno | Upload de nova versão em PDF com SHA-256. |
| `POST` | `/api/v1/theses/me/submit` | Aluno | Envia a versão atual ao orientador. |
| `POST` | `/api/v1/theses/me/withdraw` | Aluno | Descarta o envio ainda não avaliado pelo orientador (só com status `SUBMITTED`) e devolve a monografia ao estado editável. |
| `GET` | `/api/v1/theses` | Autenticado | Lista com escopo por papel e filtros (`scope`, `q`, `status`, `courseId`, `advisorId`, `year`, `pendingReview`). |
| `GET` | `/api/v1/theses/<id>` | Autor/orientador/coordenação/admin | Detalhe com versões, pareceres, documentos e histórico. |
| `POST` | `/api/v1/theses/<id>/reviews` | Orientador | Parecer: aprovar, solicitar ajustes ou reprovar. |
| `POST` | `/api/v1/theses/<id>/defense-result` | Coordenação/admin | Registra resultado final da banca. |
| `POST` | `/api/v1/theses/<id>/publish` | Coordenação/admin | Publica no repositório após validações. |
| `GET` | `/api/v1/theses/<id>/files/<fileId>/download` | Autorizado | Download/preview do PDF (`disposition=inline|attachment`). |
| `POST` | `/api/v1/theses/<id>/documents` | Autorizado | Upload de documento complementar. |
| `POST` | `/api/v1/theses/<id>/documents/<documentId>/validate` | Coordenação/admin | Valida documento complementar. |
| `GET` | `/api/v1/theses/<id>/documents/<documentId>/download` | Autorizado | Download de documento complementar. |
| `GET` | `/api/v1/public/theses` | Anônimo | Repositório público com busca/filtros e paginação. |
| `GET` | `/api/v1/public/theses/<id>` | Anônimo | Detalhe público de monografia publicada. |
| `GET` | `/api/v1/public/theses/<id>/download` | Anônimo | Download da versão publicada. |
| `GET` | `/api/v1/public/advisors` | Anônimo | Lista de orientadores (somente ativos); busca por nome, paginado (`q`, `page`, `pageSize`); sem e-mail no payload público. |
| `GET` | `/api/v1/users` | Coordenação/admin | Diretório paginado de usuários; filtro `role` (`STUDENT`/`ADVISOR`/`COORDINATOR`/`ADMIN`) e `q`; inclui e-mail. |
| `GET` | `/api/v1/users/suggest` | ADMIN/COORDINATOR/ADVISOR | Autocomplete de usuários institucionais (máx. 3): filtro `q` (mín. 2 caracteres; >80 ignorado), somente ativos, ordenado por nome/e-mail; orientador vê somente outros orientadores; coordenação/admin veem o diretório completo (ou `role=` para filtrar); throttle 60/min; resposta em cache curto (45 s). |
| `POST` | `/api/v1/auth/dev-login` | Anônimo, **só com `DEBUG=True`** | Login de teste em desenvolvimento (`name`, `email`, `roles`); sem `DEBUG` responde 404. |
| `GET` | `/api/v1/auth/dev-users` | Anônimo, **só com `DEBUG=True`** | Lista usuários ativos e perfis para o login de teste; sem `DEBUG` responde 404. |
| `GET` | `/api/v1/theses/<id>/defenses` | Institucional | Lista defesas da tese (escopo por papel). |
| `POST` | `/api/v1/theses/<id>/defenses` | Admin/coord/orientador | Agenda nova defesa; exige status `ADVISOR_APPROVED` ou `READY_FOR_DEFENSE`. |
| `GET` | `/api/v1/defenses` | Institucional | Lista paginada de bancas visíveis ao usuário; filtros `?q=`, `?status=` e `?pendingEvaluation=true`; inclui dados da tese, aluno, situação do usuário na banca (`viewerMembership`) e `viewerPermissions`. |
| `GET` | `/api/v1/defenses/schedulable-theses` | Institucional | Lista paginada de teses aptas a agendar banca (escopo de `_can_manage_defense`). |
| `GET` | `/api/v1/defenses/<id>` | Institucional ou membro da banca | Detalhe da defesa com membros, avaliações, `statusHistory` (ordem cronológica), `allMembersConfirmed`, `pendingConfirmations` (só para quem gerencia a banca), `result`/`resultLabel`, `viewerMembership` e `viewerPermissions`. |
| `PATCH` | `/api/v1/defenses/<id>` | Admin/coord/orientador | Atualiza data, duração, modalidade, local ou notas (só `SCHEDULED`); mudança de data/duração valida conflito de horário dos membros institucionais (400). |
| `POST` | `/api/v1/defenses/<id>/cancel` | Institucional | Cancela a defesa e restaura status da tese. |
| `POST` | `/api/v1/defenses/<id>/start` | Admin/coord/orientador da própria tese | Inicia a defesa (transição para `IN_PROGRESS`); exige confirmação de todos os membros não suplentes. |
| `POST` | `/api/v1/defenses/<id>/close` | Admin/coord/orientador da própria tese | Encerra a defesa, calcula resultado e transita status da tese. |
| `POST` | `/api/v1/defenses/<id>/members` | Institucional | Adiciona membro institucional à banca. |
| `POST` | `/api/v1/defenses/<id>/members/external` | Admin/coord do curso/orientador da própria tese | Cria convite externo e adiciona membro externo à banca (201); o token vai só por e-mail. |
| `POST` | `/api/v1/defenses/<id>/members/<memberId>/regenerate-token` | Admin/coord do curso/orientador da própria tese | "Gerar novo convite": revoga o convite do membro externo, cria outro e envia o token por e-mail (202). Throttle `invitation_resend`. |
| `POST` | `/api/v1/defenses/<id>/members/<memberId>/resend-invitation` | Admin/coord do curso/orientador da própria tese | Reenvia o convite ainda válido com token novo por e-mail (202); o anterior deixa de valer. Throttle `invitation_resend` (5/min). |
| `DELETE` | `/api/v1/defenses/<id>/members/<memberId>` | Institucional | Remove membro da banca (exige status `SCHEDULED` e sem avaliação). |
| `POST` | `/api/v1/defenses/<id>/members/<memberId>/availability` | O próprio membro (institucional ou convite externo) | Confirma (`CONFIRMED`) ou recusa (`DECLINED`) disponibilidade; só com a banca `SCHEDULED`. |
| `GET` | `/api/v1/defenses/<id>/members/<memberId>/evaluation` | Institucional ou membro da banca | Lê avaliação; nota/notas visíveis somente ao próprio membro. |
| `PUT` | `/api/v1/defenses/<id>/members/<memberId>/evaluation` | Membro da banca | Registra ou atualiza avaliação individual (decisão, nota 0–10, notas); a última avaliação encerra a banca automaticamente. Resposta: `evaluation`, `autoCloseError` (ou `null`), `defenseStatus`, `defenseStatusLabel`. |
| `POST` | `/api/v1/defenses/<id>/revert-status` | Admin | Reverte `IN_PROGRESS → SCHEDULED` ou `CANCELLED → status anterior`; `reason` obrigatório; `COMPLETED` não reverte. |

## 13. Prioridades sugeridas (próximos passos)

1. **Fila assíncrona**: levar extração/indexação para a fila (ver "Eventos assíncronos: melhorias futuras").
2. **Concluir integração do repositório público no frontend** e endpoint público de cursos (o de orientadores já existe).
3. **Relatórios**: exportação CSV e histórico em `report_execution`, se forem necessários; os indicadores já são calculados em `GET /api/v1/reports/overview` e exportados em PDF por `GET /api/v1/reports/overview/pdf`.
4. **Consolidar rate limiting/Redis** para uploads, busca e rastreamento de upload em andamento.
5. **Ampliar cobertura de testes** de regras, transições, permissões e contratos API.
6. **Decisões pendentes (banca)**: orientador agenda diretamente vs. solicita (Filipe); coordenador segue comportamento de admin para leitura de avaliações.

---

### Eventos assíncronos: melhorias futuras (fora do escopo de `feat/async-events`)

- **Novos eventos**: membro adicionado à banca (`defense.member_added`, para avisar cada membro, inclusive externos), banca cancelada, banca remarcada e parecer do orientador. Nenhum deles é emitido hoje.
- **Retenção**: não há limpeza de `outbox_event` publicados nem de `processed_event`; definir prazo e rotina (a FK `notification.event` usa `SET_NULL` para não travar a limpeza).
- **Reenvio automático de notificações**: hoje só manual (`--retry-pending`), por decisão do grupo.

## 14. Pendências identificadas na auditoria (2026-11-16, não implementadas)

A auditoria de 2026-11-16 encontrou os pontos abaixo, que permanecem pendentes por serem tarefas delegadas a outros desenvolvedores e não parte da refatoração de auditoria. Os pontos exatos e o contrato proposto estão anotados no código com `TODO(auditoria)` (backend: `theses/views.py`, `theses/urls.py`, `theses/services.py`, `theses/defense_committee.py`).

- **Repositório público incompleto para anônimos**: a listagem `GET /api/v1/public/theses` devolve `currentFile.downloadUrl`/`previewUrl` apontando para `/api/v1/theses/.../download`, que exige usuário autenticado (anônimo recebe 401). O detalhe público (`GET /api/v1/public/theses/<id>`) aponta para o endpoint anônimo. Uma correção mínima foi desenhada e discutida, mas não foi aplicada.
- **Sem endpoint de filtros públicos**: não existe hoje rota para listar cursos/orientadores/anos/palavras-chave presentes em publicados nem cache versionado; a busca pública não é cacheada.
- **Sem leitura de detalhe da tese para membro institucional da banca**: `theses_visible_to` e `user_can_access_thesis` não incluem o membro da comissão; ele precisa de acesso à monografia para avaliar, mas a auditoria não decidiu a regra.
- **N+1 na listagem de monografias** (revisado em 2026-11-21): autores, palavras-chave e arquivo atual já vêm por prefetch, e a listagem de bancas (`GET /api/v1/defenses`) tem número de consultas constante. Em `GET /api/v1/theses`, porém, `thesis_permissions` consulta os papéis do leitor (`user_roles`, `user_is_admin`, `user_can_access_thesis`) a cada monografia: medido com 2 e 6 itens, +3 consultas por monografia para admin e +4 para orientador. Correção esperada: resolver os papéis do leitor uma vez por página, como a listagem de bancas faz.
- **Sem rastreamento de upload em andamento** nem consolidação de rate limiting em Redis.

## Observações finais
- Itens marcados como **Implementado** referem-se ao estado observado no código; recomenda-se validar em ambiente integrado (Docker + frontend + Postgres/Redis reais) antes de considerar produção.
- Após alterações de backend, reiniciar o serviço (`docker compose restart backend`) quando o autoreload não aplicar.
- Restrições administrativas (`/api/v1/admin/*`) dependem de `ADMIN_ALLOWED_CIDRS`/configuração de rede confiável.