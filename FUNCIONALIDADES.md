# Funcionalidades — Thesis Manager UFVJM (status)

Este arquivo resume o que já está implementado e o que ainda falta no sistema, com foco no **backend** deste repositório (`thesis-manager-ufvjm-backend`). O frontend é mantido em repositório separado (`thesis-manager-ufvjm-frontend`); alguns itens abaixo indicam integração frontend já realizada quando relevante.

> Atualizado em: 2026-11-08. Este status é descritivo e pode ficar desatualizado conforme o projeto evolui.

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
| Fila de eventos (RabbitMQ/Kafka) | **Pendente** | Não há worker/broker/event bus implementado. Tarefas assíncronas (extração/indexação/notificações/envio de token) não implementadas. |
| Variáveis de ambiente / `.env` | **Implementado** | Segredos/configurações via ambiente; `.env.example` com nomes e valores fictícios. |
| Health check | **Implementado** | `GET /health` (`HealthView`). |
| Docker Compose | **Implementado** | Backend + Postgres + Redis; perfil opcional para frontend. |
| Migrations versionadas | **Implementado** | `accounts/migrations/0001_initial.py`, `academic/migrations/0001_initial.py`. |

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
| Modelos de monografia/TCC | **Pendente** | Sem app/modelos para TCC, versões, metadados, palavras-chave, status de submissão. |
| Rascunho, submissão e acompanhamento | **Pendente** | Fluxo de estados não implementado no backend. |
| Upload de PDF com validação | **Pendente** | Aceitar apenas PDF, limite configurável (ref. 50 MB), nome seguro, sem confiar em filename/MIME do cliente. |
| SHA-256 do arquivo | **Pendente** | Cálculo para integridade/deduplicação e rechecagem em aprovações/publicação. |
| CRUD de monografias com permissões/estado | **Pendente** | Endpoints autenticados/autorizados por papel e estado. |
| Busca/filtros de TCC | **Pendente** | Por título, autor/aluno, orientador, curso, ano de defesa, palavras-chave; filtros com permissão. |

## 8. Banca, avaliação e defesa

| Item | Status | Observações |
|---|---|---|
| Modelos de defesa/banca/membro/avaliação | **Pendente** | Sem modelos de agendamento, composição da banca e avaliações individuais. |
| Agendamento após aprovação do orientador | **Pendente** | Regra de negócio não implementada. |
| Disponibilidade e conflitos de horário | **Pendente** | Validação não implementada. |
| Avaliação individual por membro | **Pendente** | Membro externo/interno deve registrar própria avaliação/nota; endpoints e escopo por convite/defesa faltam. |
| Cálculo do resultado final | **Pendente** | Só após todas as avaliações requeridas; cálculo no backend. |
| Eventos de banca/avaliação | **Pendente** | Submissão, agendamento, avaliação registrada, publicação — disparo assíncrono não implementado. |

## 9. Repositório público e publicação

| Item | Status | Observações |
|---|---|---|
| Repositório público de TCCs publicados | **Pendente** | Sem endpoint público de busca/listagem de publicações. |
| Publicação condicionada | **Pendente** | Exige aprovação final, versão corrigida quando aplicável e termo de autorização assinado. |
| Proteção contra exclusão de publicado | **Pendente** | CRUD comum não pode excluir TCC publicado. |
| Recursos públicos (TCCs, cursos, orientadores) | **Parcial/Pendente** | Cursos possuem API administrativa; faltam recursos públicos/read models e TCCs/orientadores públicos conforme contrato. |

## 10. Relatórios e dashboards

| Item | Status | Observações |
|---|---|---|
| Relatórios administrativos | **Pendente** | TCCs por curso/orientador/ano, taxa de aprovação, tempo médio submissão–defesa. Cálculo deve ser no backend. |
| Dashboards frontend | **Parcial** | Telas/rotas existem no frontend (parte com dados mock/integração incompleta, dependendo da área). |

## 11. Frontend (repositório separado, resumo de integração)

| Item | Status | Observações |
|---|---|---|
| Login Google + sessão | **Implementado** | Integração com `/api/v1/session` e início OAuth. |
| Login por token externo | **Implementado** | Troca de token e rota para avaliação externa. |
| Área admin: usuários/cursos/auditoria | **Implementado** | Integrada aos endpoints administrativos; multi-papéis e navegação combinada. |
| Criação de convite externo | **Implementado** | Admin e tela de bancas (integração parcial; escopo de banca pendente). |
| Fluxos de TCC/banca/avaliação/repositório/relatórios | **Parcial/Pendente** | Estruturas de UI existem em várias áreas, mas dependem de domínio/backend de TCC, banca, avaliação e publicação. |

## 12. Prioridades sugeridas (próximos passos)

1. **Modelar domínio de TCC/defesa/banca/avaliação** (migrations, regras de estado, permissões).
2. **Escopar convites externos** à defesa/membro da banca e implementar avaliação do membro externo com nota.
3. **Upload seguro de PDF** (validação, armazenamento, SHA-256, autorização de download).
4. **Fila assíncrona** (eventos, extração/indexação, notificações, envio de token/e-mail).
5. **Repositório público e publicação** com regras de elegibilidade e busca.
6. **Relatórios** calculados no backend.
7. **Cobertura de testes** para domínios novos (regras, transições, permissões, contratos API).

---

## Observações finais
- Itens marcados como **Implementado** referem-se ao estado observado no código; recomenda-se validar em ambiente integrado (Docker + frontend + Postgres/Redis reais) antes de considerar produção.
- Após alterações de backend, reiniciar o serviço (`docker compose restart backend`) quando o autoreload não aplicar.
- Restrições administrativas (`/api/v1/admin/*`) dependem de `ADMIN_ALLOWED_CIDRS`/configuração de rede confiável.