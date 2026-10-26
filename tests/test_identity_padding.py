"""Padded identifiers must not alias a different MySQL unique-key owner."""
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.presentation.auth import resolve_buyer_keys


@pytest.mark.parametrize('buyer', ['alice ', ' alice', '\talice', 'alice\n'])
def test_identity_config_rejects_padded_buyer_names(buyer):
    container = SimpleNamespace(settings=SimpleNamespace(buyer_api_keys_json=json.dumps({
        'alice': 'alice-unpadded-test-key', buyer: 'different-padded-test-key',
    })))
    with pytest.raises(HTTPException) as error:
        resolve_buyer_keys(container)
    assert error.value.status_code == 503
