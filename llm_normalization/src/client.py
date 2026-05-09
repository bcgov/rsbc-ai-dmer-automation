"""
Azure OpenAI client initialization.
====================================
Reads connection details from environment variables (loaded via .env)
and exposes a configured ``AzureOpenAI`` client instance.
"""

import os

from dotenv import load_dotenv
from openai import AzureOpenAI

load_dotenv()

ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"]
API_KEY = os.environ["AZURE_OPENAI_API_KEY"]
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

print(DEPLOYMENT)

client = AzureOpenAI(
    azure_endpoint=ENDPOINT,
    api_key=API_KEY,
    api_version=API_VERSION,
)
