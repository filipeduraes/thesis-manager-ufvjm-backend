"""
Signal receivers that protect published theses from deletion.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from theses.models import PUBLISHED_DELETE_ERROR, Thesis, thesis_was_published


@receiver(pre_delete, sender=Thesis, dispatch_uid="theses_block_published_delete")
def block_published_thesis_delete(sender: type[Thesis], instance: Thesis, **kwargs: object) -> None:
    """
    Refuse the deletion of a thesis that is or was published.

    ``Thesis.delete`` covers single objects; this receiver also covers
    ``QuerySet.delete()``, cascades and the Django admin, because a model with a
    ``pre_delete`` receiver is never fast-deleted.

    Args:
        sender: Thesis model.
        instance: Thesis about to be deleted.
        **kwargs: Signal arguments.

    Raises:
        ValidationError: When the thesis is or was published.
    """
    if thesis_was_published(instance):
        raise ValidationError(PUBLISHED_DELETE_ERROR)
