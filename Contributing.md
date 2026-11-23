# Padrões de Código

Este guia é para quem vai desenvolver no backend. A ideia é que você consiga entrar em qualquer app e saber onde colocar uma regra, como nomear as coisas e o que precisa ser testado. Quando o código existente divergir deste guia, siga o estilo do arquivo que você está editando e corrija a divergência aos poucos.

---

## 1. Como o código se organiza

O projeto é Django com Django REST Framework. Cada app tem uma responsabilidade clara. Contas cuidam de identidade, papéis, sessão e auditoria. Acadêmico cuida de cursos e departamentos. O app de monografias concentra teses, bancas, avaliações, upload e repositório. Eventos, notificações, indexação e adjudicação cuidam do trabalho assíncrono. Relatórios e os dois read models fecham o conjunto.

A separação mais importante é entre camadas. O model guarda estado e regras que dependem dele. O service concentra o fluxo de negócio. O serializer valida o que chegou pela API. A view traduz isso para HTTP. Não coloque regra de negócio dentro da view nem confie em papel, status ou autoria enviados pelo cliente.

Quando uma operação muda estado e precisa gerar um evento, as duas coisas acontecem na mesma transação. O produtor nunca conversa com o RabbitMQ diretamente. Ele grava na tabela de eventos e o publicador faz o resto.

O exemplo abaixo mostra um service típico. Repare na assinatura anotada, na docstring em inglês no formato Google, na validação antes da escrita, no evento gravado logo depois da mudança e no log com identificadores:

```python
from __future__ import annotations

import logging

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.models import User
from theses.events import record_thesis_submitted
from theses.models import Thesis, ThesisStatus

logger = logging.getLogger(__name__)


def submit_thesis(thesis: Thesis, actor: User, version_number: int) -> Thesis:
    """
    Move an editable thesis to the advisor review queue.

    Args:
        thesis: Thesis being submitted.
        actor: Student author performing the action.
        version_number: Number of the submitted file version.

    Returns:
        Thesis: Thesis with the submitted state persisted.

    Raises:
        ValidationError: When the thesis is not in an editable state.
    """
    if thesis.status != ThesisStatus.DRAFT:
        raise ValidationError("Only draft theses can be submitted.")

    previous_status = thesis.status
    with transaction.atomic():
        thesis.status = ThesisStatus.SUBMITTED
        thesis.save(update_fields=["status", "updated_at"])
        record_thesis_submitted(thesis, version_number)

    logger.info(
        "Thesis %s submitted by user %s from status %s",
        thesis.pk,
        actor.pk,
        previous_status,
    )
    return thesis
```

## 2. Nomeação

Use nomes descritivos e consistentes. A tabela abaixo mostra o padrão que o código já segue.

| Item | Regra | Exemplo |
|---|---|---|
| Classe | PascalCase | ThesisService, ReportsOverviewView |
| Função e método | snake_case | submit_thesis, publish_if_ready |
| Função interna | começa com underscore | _validate_pdf_upload |
| Constante | UPPER_SNAKE_CASE | PDF_MAGIC_BYTES, PAYLOAD_KEYS |
| Modelo | substantivo singular | Thesis, DefenseMember |
| Campo do model | snake_case, booleanos começam com is, datas terminam com at | is_public, submitted_at |
| Status | classe TextChoices, membro em maiúsculas, rótulo em português | DRAFT = "DRAFT", "Rascunho" |
| Serializer | termina com Serializer | ThesisReviewSerializer |
| Permissão | começa com Is ou Can | IsStudentUser, CanViewReports |
| Módulo | substantivo singular por responsabilidade | repository_cache, defense_scheduling |
| Teste | arquivo test_domínio, classe RecursoTests, método test_ação_resultado | test_submit_returns_400_when_draft_is_missing |

Evite nomes como data, obj e helper. Se você souber o que a variável representa, use esse nome.

O exemplo de model abaixo mostra essas regras aplicadas. O valor do Choice é o código que vai na API, e o rótulo fica em português para a interface:

```python
from __future__ import annotations

from uuid import uuid4

from django.db import models


class ThesisStatus(models.TextChoices):
    DRAFT = "DRAFT", "Rascunho"
    SUBMITTED = "SUBMITTED", "Em avaliação"
    APPROVED = "APPROVED", "Aprovada"


class Thesis(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    title = models.CharField(max_length=300)
    status = models.CharField(max_length=40, choices=ThesisStatus.choices, default=ThesisStatus.DRAFT, db_index=True)
    is_public = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "thesis"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-updated_at"], name="thesis_status_updated_idx")]
```

## 3. Legibilidade e tipos

Anote parâmetros e retorno de toda função nova ou alterada. Prefira tipos concretos. Use list[str], dict[str, int] e QuerySet[Thesis] em vez de estruturas vagas. Quando não existir tipo melhor, use Any e escreva um comentário explicando o motivo. Isso acontece com objetos do DRF quando não há django-stubs instalado.

Mantenha a assinatura em uma linha. Se ela ficar longa, agrupe parâmetros relacionados em um objeto pequeno. Não encadeie chamadas complexas na mesma linha. Guarde resultados intermediários em variáveis com nomes claros.

Todo módulo Python começa com a importação futura de anotações, logo depois da docstring. Se um import existe apenas para anotar tipos, coloque-o dentro de um bloco TYPE_CHECKING. Antes de fazer isso, pare e pense na arquitetura: por que esse módulo precisa conhecer aquele outro? Permissões e limitadores, por exemplo, são carregados pelo DRF durante a inicialização e não devem depender de views em runtime. Se aparecer import circular, procure primeiro uma responsabilidade melhor colocada. Use TYPE_CHECKING quando a dependência for realmente só de tipo e você tiver confirmado que o nome não é chamado em runtime, usado em isinstance ou como construtor. Parece só uma anotação e quebra tudo na hora de importar.

Docstrings novas são sempre em inglês, no formato Google. Use Args, Returns e Raises quando fizer sentido. As aspas ficam em linhas próprias, sem linha em branco entre elas e o texto. Comentários para quem desenvolve ficam em português e explicam o porquê de uma decisão. Logs ficam em inglês e levam identificadores, nunca conteúdo sensível.

Não use print para diagnóstico de aplicação. Não registre token, senha, chave, URL de conexão, conteúdo de documento ou payload completo.

## 4. API e contrato

A API é versionada e fala JSON. Campos de requisição e resposta usam camelCase, mesmo quando o model usa snake_case. O serializer faz essa tradução com o argumento source.

Use paginação em listas que podem crescer. O envelope traz count, page, pageSize, totalPages, lastPageCount e results. Erros usam statusHTTP coerentes e mensagens em português. Uma view declara suas permissões e seu limite de requisições como atributos de classe.

O exemplo abaixo mostra um serializer que valida dados do cliente e expõe um campo da API com outro nome no model:

```python
from __future__ import annotations

from rest_framework import serializers

from academic.models import Course
from theses.models import Thesis


class ThesisDraftSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=300, trim_whitespace=True)
    abstract = serializers.CharField(max_length=5000, trim_whitespace=True)
    course_id = serializers.PrimaryKeyRelatedField(
        source="course",
        queryset=Course.objects.all(),
        write_only=True,
    )

    def validate_title(self, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 10:
            raise serializers.ValidationError("O título deve ter ao menos 10 caracteres.")
        return normalized
```

## 5. Banco, transações e fronteiras de serviço

Cada serviço é dono das suas tabelas. O roteador decide onde cada model grava. Quando os bancos estão separados, não existe JOIN entre eles. Para usar um identificador de outro serviço, faça uma consulta separada no banco dono e filtre a coluna local terminada em id.

Não atribua instância de outro serviço a um model. Escreva course_id, advisor_id e assim por diante. Transação que trava linha abre no alias dono, com atomic informando o banco. Grave a linha do serviço antes do marcador de idempotência do consumidor.

Crie migração junto com a mudança de model. Não edite migração já aplicada como se fosse descartável.

## 6. Eventos e trabalho assíncrono

Trabalho demorado nunca acontece dentro da requisição HTTP. Extração de texto, e-mail, cálculo de resultado e projeções passam pela fila.

O catálogo de eventos é a fonte única do contrato. Um payload leva identificadores e códigos, nunca nome, e-mail, nota, decisão antes do fechamento ou token. Se precisar mudar a forma de um payload existente, crie uma nova versão.

Consumidores são idempotentes. O marcador de processamento e a linha de notificação entram juntos. E-mail sai depois do commit. Em caso de mensagem inválida, vai para a dead letter queue. Falha de banco é o único caso que volta para a fila.

## 7. Segurança

Segredo nunca fica no código. Chave, senha de banco, URL do broker e credenciais de e-mail vêm de variáveis de ambiente. O arquivo de exemplo traz só nomes e valores fictícios.

A área administrativa depende de configuração de rede. Cabeçalho de proxy só entra na decisão quando o proxy foi configurado. Não confie em endereço enviado pelo cliente.

Upload aceita PDF com assinatura e marcador final, limita tamanho, gera nome seguro e calcula SHA-256. Nome e tipo enviados pelo navegador não são prova de nada. Download passa por verificação de permissão.

Toda ação relevante gera auditoria com quem fez, o que fez e quando. A tabela é append-only e encadeada por hash. Não copie conteúdo sensível para ela nem para os logs.

## 8. Testes

Toda mudança vem com teste. Escreva o teste primeiro e confirme que ele falha. Depois implemente e faça ele passar. Não enfraquecer asserção para a suíte ficar verde.

Use o caminho real do domínio nas fixtures. Se um teste precisa de uma banca encerrada, passe por agendamento, confirmação, início, avaliação e fechamento. Atalhos que só trocam o status escondem defeitos.

```python
from __future__ import annotations

from django.test import TestCase

from theses.models import Thesis, ThesisStatus
from theses.services import submit_thesis


def make_thesis() -> Thesis:
    return Thesis.objects.create(title="Sistemas distribuídos em escala", status=ThesisStatus.DRAFT)


class ThesisSubmissionTests(TestCase):
    def setUp(self) -> None:
        self.thesis = make_thesis()

    def test_submit_moves_draft_to_submitted(self) -> None:
        result = submit_thesis(self.thesis, self.user, 1)

        self.assertEqual(result.status, ThesisStatus.SUBMITTED)
        self.assertEqual(Thesis.objects.get(pk=result.pk).status, ThesisStatus.SUBMITTED)
```

O comando rápido roda a cada mudança. O completo roda antes de fechar uma tarefa maior. Mudança que altera o que o usuário vê também pede o teste de ponta a ponta correspondente no repositório do frontend.

## 9. Antes de entregar

- Rode a verificação de configuração do Django.
- Confira se não existe migração pendente.
- Rode os testes relevantes e explique qualquer verificação que não foi possível executar.
- Atualize a documentação que a mudança afetou.
- Não versione ambiente local, banco, upload, cache nem artefato gerado.

## 10. Boas práticas gerais

Prefira funções pequenas, com uma responsabilidade. Evite estado global mutável e abstração sem uso concreto. Não desabilite validação, teste ou verificação para esconder falha. Comente o porquê, não o óbvio. Código claramente escrito vale mais do que uma linha esperta.
