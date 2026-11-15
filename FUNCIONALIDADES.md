# Funcionalidades — Thesis Manager UFVJM (status)

Este arquivo resume o que já está implementado e o que ainda falta no sistema, com foco no **backend** deste repositório (`thesis-manager-ufvjm-backend`). O frontend é mantido em repositório separado (`thesis-manager-ufvjm-frontend`); alguns itens abaixo indicam integração frontend já realizada quando relevante.

> Atualizado em: 2026-11-09. Este status é descritivo e pode ficar desatualizado conforme o projeto evolui.

## Legenda
- **Implementado**: funcionalidade disponível no backend (e, quando indicado, integrada no frontend).
- **Parcial**: base/endpoints existem, mas faltam regras de domínio, escopo, UI de fluxo ou integrações.
- **Pendente**: não implementado.

---

## 1. Infraestrutura e base do projeto

| Item | Status | Observações |
|---|---|---|
| Django + DRF + Python | **Implementado** | Projeto `thesismanagerbackend`; apps `accounts` e `academic`. |
| PostgreSQL | **Implementado** | Configuração via `DATABASE_URL`; Docker Compose com serviço `db`. |
| Redis | **Parcial** | `REDIS_URL`/cache configurados (`CACHES`, sessão `cached_db`); falta validar/implementar todos os usos previstos (cache de busca/sessão, rastreamento de upload em andamento, rate limiting completo conforme requisitos). |
| Fila de eventos (RabbitMQ/Kafka) | **Pendente** | Não há worker/broker/event bus implementado. Upload marca `processing_status=PENDING` e existe o comando `extract_thesis_text` para extração sob demanda; extração/indexação/notificações automáticas e envio de token/e-mail seguem pendentes. |
| Variáveis de ambiente / `.env` | **Implementado** | Segredos/configurações via ambiente; `.env.example` com nomes e valores fictícios (inclui `THESIS_MAX_PDF_SIZE_MB`, `THESIS_MAX_DOCUMENT_SIZE_MB`, `THESIS_MAX_KEYWORDS`). |
| Health check | **Implementado** | `GET /health` (`HealthView`). |
| Docker Compose | **Implementado** | Backend + Postgres + Redis; perfil opcional para frontend. |
| Migrations versionadas | **Implementado** | `accounts/migrations/0001_initial.py`, `academic/migrations/0001_initial.py`, `theses/migrations/0001_initial.py`. |

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
| Modelo de convite externo | **Implementado** | `ExternalInvitation`: dados do convidado, `token_hash` SHA-256, status, expiração, uso/revogação; sem armazenar token legível. |
| Geração de token temporário | **Implementado** | Token humano `UFVJM-XXXX-...`; `POST /api/v1/admin/external-invitations` cria convite e retorna token **uma vez**. TTL configurável. |
| Restrição de cargos para criar convite | **Implementado** | Criação permitida para **Administrador, Coordenador, Orientador** (`CanInviteExternalCommitteeMember`). Listagem global e revogação permanecem administrativas. |
| Listar/revogar convites | **Implementado** | `GET /api/v1/admin/external-invitations` (admin), `DELETE /api/v1/admin/external-invitations/<id>` (admin). |
| Troca de token por sessão externa | **Implementado** | `POST /api/v1/auth/external-token`; valida hash/estado/expiração, `flush` da sessão, TTL da sessão externa, auditoria `TOKEN_ACCESS`. |
| Autenticação de sessão externa | **Implementado** | `ExternalInvitationSessionAuthentication` + `ExternalInvitationPrincipal`; CSRF em métodos não seguros; bloqueio por expiração/revogação. |
| Login por token no frontend | **Implementado** | `LoginView` envia token; sessão externa direciona para fluxo de avaliação externa. |
| Criação de token no frontend | **Implementado** | UI admin (`AdminUsers`) e integração na tela de bancas/agendamento (`DefenseScheduling`) para gerar token real (sem vínculo de defesa ainda). |
| Escopo do convite à banca/defesa/membro | **Pendente/Parcial** | Convite ainda não é vinculado a `defense`/membro da banca/TCC; necessário para garantir acesso apenas à banca correta. |
| Envio do token por e-mail | **Pendente** | Sem fila/worker/notificação; planejamento discutido (assíncrono, sem token em logs/banco). |

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
| Modelos de defesa/banca/membro/avaliação | **Implementado** | `Defense`, `DefenseMember`, `DefenseMemberEvaluation` com `DefenseStatus`, `CommitteeRole`, `DefenseResult`, `DefenseModality`; migration 0002 + 0003 (`duration_minutes`). |
| Agendamento após aprovação do orientador | **Implementado** | `POST /api/v1/theses/<id>/defenses` exige `ADVISOR_APPROVED` ou `READY_FOR_DEFENSE`; admin/coordenador/orientador da própria tese podem agendar; transições de `ThesisStatus` e histórico registrados. Nota: orientador agenda diretamente (sem "solicitação") — pendente decisão do Filipe. |
| Disponibilidade e conflitos de horário | **Implementado** | Verificação de sobreposição de horário por membro antes de adicionar à banca (intervalo Python: `A.start < B.end AND B.start < A.end`); membro pode fornecer `conflictReason` para registrar ciência do conflito. |
| Avaliação individual por membro | **Implementado** | `PUT /api/v1/defenses/<id>/members/<memberId>/evaluation` — cada membro (institucional ou externo via convite válido) registra decisão, nota (0–10) e notas; suplentes não podem avaliar (403); membro não vê nota/notas de outro membro. |
| Encerramento automático com cálculo de resultado | **Implementado** | `POST /api/v1/defenses/<id>/close` (admin/coordenador) verifica quórum, calcula resultado por maioria, transita `ThesisStatus` e chama `purge_thesis_versions` para aprovados. |
| Registro do resultado final (manual) | **Implementado** | `POST /api/v1/theses/<id>/defense-result` (coordenação/admin) ainda disponível para lançamento manual retroativo. |
| Abertura automática para avaliação | **Implementado** | `sync_defense_status` transita `SCHEDULED → IN_PROGRESS` quando `scheduled_datetime` já passou, sem job assíncrono. Chamada no início dos endpoints de detalhe, membros e avaliação. Idempotente (audit event `AUTO_STARTED_BY_SCHEDULE`; `AuditAction.UPDATE`). Teses transitam para `UNDER_EVALUATION` pelo mesmo código-caminho do `start_defense` manual. |
| Reversão de status pelo administrador | **Implementado** | `POST /api/v1/defenses/<id>/revert-status` (somente admin). Permite `IN_PROGRESS → SCHEDULED` e `CANCELLED → status anterior` (campo `previous_status` em `Defense`; fallback para `SCHEDULED`). `COMPLETED` não reverte (400). Campo `auto_start_suspended` impede reabertura automática até que o horário seja reagendado via `PATCH` ou a banca seja iniciada manualmente. `reason` obrigatório (400 se vazio). Audit event `STATUS_REVERTED_BY_ADMIN`. Migration `0004_add_defense_revert_fields`. |
| Listagem de bancas visíveis ao usuário | **Implementado** | `GET /api/v1/defenses` (paginado, `?status=` com códigos `DefenseStatus` separados por vírgula; códigos inválidos são ignorados). Escopo em `theses/defense_listing.py::defenses_visible_to` (mesma regra do detalhe): admin/coordenador veem todas; orientador vê as das teses que orienta e as que integra; aluno vê a da própria tese; membro institucional vê as que integra. Sessão de convite externo recebe 403. Cada item = `defense_payload` + `thesisTitle`, `thesisReferenceCode`, `student`, `advisor`, `viewerMembership` (`isMember`, `memberId`, `role`, `roleLabel`, `canEvaluate`, `hasEvaluated`). Bancas agendadas com horário vencido são iniciadas (`sync_defense_status`) antes do filtro. |
| Teses aptas a agendar banca | **Implementado** | `GET /api/v1/defenses/schedulable-theses` (paginado): status `ADVISOR_APPROVED`/`READY_FOR_DEFENSE`, sem banca `SCHEDULED`/`IN_PROGRESS`, regra de `_can_manage_defense` (admin/coord todas; orientador só as próprias; demais perfis recebem lista vazia; convite externo 403). Itens no formato de `GET /api/v1/theses`. |
| Eventos de banca/avaliação | **Pendente** | Submissão, agendamento, avaliação registrada, publicação — disparo assíncrono não implementado; histórico e auditoria síncronos já registram as transições. |

## 9. Repositório público e publicação

| Item | Status | Observações |
|---|---|---|
| Repositório público de TCCs publicados | **Implementado** | `GET /api/v1/public/theses` (busca por título/resumo/autor/orientador/palavra-chave e filtros de curso/orientador/ano) e `GET /api/v1/public/theses/<id>` com payload público; acesso anônimo somente a publicados. |
| Publicação condicionada | **Implementado** | `POST /api/v1/theses/<id>/publish` exige aprovação final, autorização de publicação validada e arquivo atual íntegro; remove versões antigas e publica apenas a versão atual. |
| Proteção contra exclusão de publicado | **Implementado** | Não há endpoint de exclusão de monografia; publicação/arquivamento são transições controladas e o histórico/auditoria é preservado. |
| Recursos públicos (TCCs, cursos, orientadores) | **Parcial** | TCCs publicados e download estão disponíveis publicamente; faltam endpoints públicos dedicados de cursos e orientadores para filtros do repositório. |

## 10. Relatórios e dashboards

| Item | Status | Observações |
|---|---|---|
| Relatórios administrativos | **Pendente** | TCCs por curso/orientador/ano, taxa de aprovação, tempo médio submissão–defesa. Cálculo deve ser no backend. |
| Dashboards frontend | **Parcial** | Dashboard do aluno (status, progresso, próxima ação, pareceres e atividades) e do orientador (fila de revisão e orientandos) usam dados reais de TCC. Demais áreas (coordenação/admin) ainda usam dados mock em parte. |

## 11. Frontend (repositório separado, resumo de integração)

| Item | Status | Observações |
|---|---|---|
| Login Google + sessão | **Implementado** | Integração com `/api/v1/session` e início OAuth. |
| Login por token externo | **Implementado** | Troca de token e rota para avaliação externa. |
| Área admin: usuários/cursos/auditoria | **Implementado** | Integrada aos endpoints administrativos; multi-papéis e navegação combinada. |
| Criação de convite externo | **Implementado** | Admin e tela de bancas (integração parcial; escopo de banca pendente). |
| Fluxos de TCC/orientação | **Implementado** | Submissão do aluno, acompanhamento, upload de PDF/versões/documentos, parecer do orientador (aprovar/ajustes/reprovar), leitor de PDF com zoom/download, resultado da banca e publicação. |
| Fluxos de banca/avaliação/repositório/relatórios | **Parcial/Pendente** | Agendamento de banca, avaliação individual e relatórios dependem de domínio/backend pendente; repositório público possui endpoint, mas a tela do frontend ainda usa dados mock. |

## 12. Endpoints de TCC implementados

| Método | Rota | Acesso | Observação |
|---|---|---|---|
| `GET` | `/api/v1/theses/me` | Aluno | Contexto da monografia, opções de curso/orientador e limites. |
| `PUT` | `/api/v1/theses/me` | Aluno | Cria/atualiza rascunho e metadados. |
| `POST` | `/api/v1/theses/me/files` | Aluno | Upload de nova versão em PDF com SHA-256. |
| `POST` | `/api/v1/theses/me/submit` | Aluno | Envia a versão atual ao orientador. |
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
| `GET` | `/api/v1/theses/<id>/defenses` | Institucional | Lista defesas da tese (escopo por papel). |
| `POST` | `/api/v1/theses/<id>/defenses` | Admin/coord/orientador | Agenda nova defesa; exige status `ADVISOR_APPROVED` ou `READY_FOR_DEFENSE`. |
| `GET` | `/api/v1/defenses` | Institucional | Lista paginada de bancas visíveis ao usuário; filtro `?status=`; inclui dados da tese, aluno e situação do usuário na banca. |
| `GET` | `/api/v1/defenses/schedulable-theses` | Institucional | Lista paginada de teses aptas a agendar banca (escopo de `_can_manage_defense`). |
| `GET` | `/api/v1/defenses/<id>` | Institucional ou membro da banca | Detalhe da defesa com membros e avaliações. |
| `PATCH` | `/api/v1/defenses/<id>` | Admin/coord/orientador | Atualiza data, duração, modalidade, local ou notas. |
| `POST` | `/api/v1/defenses/<id>/cancel` | Institucional | Cancela a defesa e restaura status da tese. |
| `POST` | `/api/v1/defenses/<id>/start` | Admin/coord | Inicia a defesa (transição para `IN_PROGRESS`). |
| `POST` | `/api/v1/defenses/<id>/close` | Admin/coord | Encerra a defesa, calcula resultado e transita status da tese. |
| `POST` | `/api/v1/defenses/<id>/members` | Institucional | Adiciona membro institucional à banca. |
| `POST` | `/api/v1/defenses/<id>/members/external` | Institucional | Cria convite externo e adiciona membro externo à banca. |
| `DELETE` | `/api/v1/defenses/<id>/members/<memberId>` | Institucional | Remove membro da banca (exige status `SCHEDULED` e sem avaliação). |
| `GET` | `/api/v1/defenses/<id>/members/<memberId>/evaluation` | Institucional ou membro da banca | Lê avaliação; nota/notas visíveis somente ao próprio membro. |
| `PUT` | `/api/v1/defenses/<id>/members/<memberId>/evaluation` | Membro da banca | Registra ou atualiza avaliação individual (decisão, nota 0–10, notas). |
| `POST` | `/api/v1/defenses/<id>/revert-status` | Admin | Reverte `IN_PROGRESS → SCHEDULED` ou `CANCELLED → status anterior`; `reason` obrigatório; `COMPLETED` não reverte. |

## 13. Prioridades sugeridas (próximos passos)

1. **Fila assíncrona** (eventos de submissão/agendamento/avaliação/publicação, extração/indexação, notificações e envio de token/e-mail).
2. **Concluir integração do repositório público no frontend** e endpoints públicos de cursos/orientadores.
3. **Relatórios** calculados no backend (por curso/orientador/ano, taxa de aprovação, tempo médio submissão–defesa).
4. **Consolidar rate limiting/Redis** para uploads, busca e rastreamento de upload em andamento.
5. **Ampliar cobertura de testes** de regras, transições, permissões e contratos API.
6. **Decisões pendentes (banca)**: orientador agenda diretamente vs. solicita (Filipe); `start_defense`/`close_defense` restritos a admin/coord (Filipe); coordenador segue comportamento de admin para leitura de avaliações.

---

## Observações finais
- Itens marcados como **Implementado** referem-se ao estado observado no código; recomenda-se validar em ambiente integrado (Docker + frontend + Postgres/Redis reais) antes de considerar produção.
- Após alterações de backend, reiniciar o serviço (`docker compose restart backend`) quando o autoreload não aplicar.
- Restrições administrativas (`/api/v1/admin/*`) dependem de `ADMIN_ALLOWED_CIDRS`/configuração de rede confiável.