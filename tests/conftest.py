import os
from pathlib import Path

import pytest
import tiktoken

from goalkeeper.cost import Tokens

os.environ["TIKTOKEN_CACHE_DIR"] = str(Path(__file__).resolve().parents[1] / "work/tokenizer-cache")


@pytest.fixture
def tokens():
    return Tokens(tiktoken.get_encoding("o200k_base"))
