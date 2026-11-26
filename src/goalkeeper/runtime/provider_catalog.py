"""Desktop API-key routes; deliberately excludes OAuth and cloud deployment setup.

OpenAI model IDs mirror the Codex desktop selector requested for this build.
Protocols/endpoints checked against provider documentation, 2026-10-13.
Only these reviewed models are selectable; remote catalog data is never executable config.
"""
OPENAI_MODELS = [
    'gpt-6.1-sol',
    'gpt-6-astra',
    'gpt-6-sol',
    'gpt-6-luna',
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
]

OPENAI_EFFORTS = ['low', 'medium', 'high', 'xhigh']

# Account-backed execution uses the same reviewed model range as the Codex
# desktop selector. A separate provider id keeps an additionally connected
# ChatGPT account distinguishable from the primary GoalKeeper account.
ACCOUNT_MODELS = OPENAI_MODELS
ACCOUNT_PROVIDER_IDS = {'account', 'chatgpt_account'}
ACCOUNT_CATALOG = {
    'chatgpt_account': {
        'name': 'OpenAI / ChatGPT 账号',
        'models': ACCOUNT_MODELS,
        'auth': 'oauth_pkce',
    },
}


def is_account_provider(provider):
    return provider in ACCOUNT_PROVIDER_IDS


CATALOG = {
    'openai': {'name': 'OpenAI', 'base_url': 'https://api.openai.com/v1',
               'protocol': 'responses', 'models': OPENAI_MODELS, 'efforts': OPENAI_EFFORTS,
               'default_effort': 'medium', 'docs': 'https://platform.openai.com/api-keys'},
    'deepseek': {'name': 'DeepSeek', 'base_url': 'https://api.deepseek.com',
                 'models': ['deepseek-v4-pro', 'deepseek-flash'], 'docs': 'https://platform.deepseek.com/api_keys'},
    'kimi': {'name': 'Kimi', 'base_url': 'https://api.moonshot.cn/v1',
             'models': ['kimi-k2.6'], 'docs': 'https://platform.kimi.com/docs/get-api-key'},
    'anthropic': {'name': 'Anthropic', 'base_url': 'https://api.anthropic.com/v1', 'protocol': 'anthropic',
                  'models': ['claude-sonnet-4-6', 'claude-haiku-4-5-20251001'], 'docs': 'https://platform.claude.com/settings/keys'},
    'google': {'name': 'Google Gemini', 'base_url': 'https://generativelanguage.googleapis.com/v1beta/openai',
               'models': ['gemini-2.5-flash', 'gemini-2.5-flash-lite'], 'docs': 'https://aistudio.google.com/api-keys'},
    'openrouter': {'name': 'OpenRouter', 'base_url': 'https://openrouter.ai/api/v1',
                   'models': ['openai/gpt-4.1', 'openai/gpt-4.1-mini'], 'docs': 'https://openrouter.ai/settings/keys'},
    'groq': {'name': 'Groq', 'base_url': 'https://api.groq.com/openai/v1',
             'models': ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant'], 'docs': 'https://console.groq.com/keys'},
    'mistral': {'name': 'Mistral', 'base_url': 'https://api.mistral.ai/v1',
                'models': ['mistral-large-2512', 'devstral-2512'], 'docs': 'https://console.mistral.ai/api-keys'},
    'together': {'name': 'Together AI', 'base_url': 'https://api.together.ai/v1',
                 'models': ['meta-llama/Llama-3.3-70B-Instruct-Turbo', 'Qwen/Qwen2.5-7B-Instruct-Turbo'], 'docs': 'https://api.together.ai/settings/api-keys'},
    'huggingface': {'name': 'Hugging Face', 'max_tokens': 4096, 'base_url': 'https://router.huggingface.co/v1',
                    'models': ['meta-llama/Llama-3.3-70B-Instruct'], 'docs': 'https://huggingface.co/settings/tokens'},
    'nvidia': {'name': 'NVIDIA', 'base_url': 'https://integrate.api.nvidia.com/v1',
               'models': ['nvidia/llama-3.1-nemotron-70b-instruct'], 'docs': 'https://build.nvidia.com'},
    'moonshotai': {'name': 'Kimi（国际）', 'base_url': 'https://api.moonshot.ai/v1',
                   'models': ['kimi-k2.6'], 'docs': 'https://platform.kimi.ai/docs/get-api-key'},
    'zai': {'name': 'Z.AI', 'base_url': 'https://api.z.ai/api/paas/v4',
            'models': ['glm-4.7'], 'docs': 'https://open.bigmodel.cn/usercenter/proj-mgmt/apikeys'},
}


def response_model_matches(provider, expected, actual):
    """Normalize only documented, family-specific aliases; never accept a different model."""
    import re
    if actual == expected:
        return True
    if not isinstance(actual, str):
        return False
    if provider == 'openai':
        return bool(re.fullmatch(re.escape(expected) + r'-\d{4}-\d{2}-\d{2}', actual))
    if provider == 'openrouter' and expected.startswith('openai/'):
        family = expected.removeprefix('openai/')
        return actual == family or bool(re.fullmatch(r'(?:openai/)?' + re.escape(family) + r'-\d{4}-\d{2}-\d{2}', actual))
    if provider == 'google':
        return actual == 'models/' + expected
    return False
