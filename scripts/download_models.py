from huggingface_hub import snapshot_download

MODELS = [
    ("NeoQuasar/Kronos-small", "901c26c1332695a2a8f243eb2f37243a37bea320"),
    ("NeoQuasar/Kronos-Tokenizer-base", "f6c7de7b0490e422fde7a44bc73369e07115c445"),
]
for repo_id, revision in MODELS:
    path = snapshot_download(repo_id=repo_id, revision=revision)
    print(f"Downloaded {repo_id}@{revision} to {path}")
