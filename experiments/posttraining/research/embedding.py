"""Original neural embedder, with one persistent cache rather than episode copies."""

import os
from pathlib import Path
from unittest.mock import patch


def create_reference_embedder(logger, tracer, meter, model_path, cache_path):
    from apps.retail_demo.nlp import LocalEmbedder
    from parlant.adapters.nlp import hugging_face as native

    Path(cache_path).mkdir(parents=True, exist_ok=True)
    # The original implementation saves a model under PARLANT_HOME. Prewarm
    # those same native caches once per process in a shared weights cache;
    # restore episode storage before any APP/session service is constructed.
    with patch.dict(os.environ, PARLANT_HOME=str(cache_path), DEMO_EMBEDDING_MODEL=str(model_path)):
        native._create_tokenizer(str(model_path))
        native._create_auto_model(str(model_path))
        return LocalEmbedder(logger, tracer, meter)
