import openai
import json
import argparse

with open('config/api_models.json', 'r') as f:
    api_models = json.load(f)

parser = argparse.ArgumentParser()
parser.add_argument('--model', type=str, required=True)
args = parser.parse_args()

"""
{
    "gemini3flash":{
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "<YOUR_API_KEY>",
        "modelname": "google/gemini-3-flash-preview",
        "model_params": {
            "extra_body": {
                "provider": {"only": ["google-vertex"]}
            }
        }
    },
    "gemini3flashlite":{
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "<YOUR_API_KEY>",
        "modelname": "google/gemini-3.1-flash-lite-preview",
        "model_params": {
            "extra_body": {
                "provider": {"only": ["google-vertex"]}
            }
        }
    },
    "gemini31pro":{
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "<YOUR_API_KEY>",
        "modelname": "google/gemini-3.1-pro-preview",
        "model_params": {
            "extra_body": {
                "provider": {"only": ["google-vertex"]}
            }
        }
    },
    "minimax2.5":{
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "<YOUR_API_KEY>",
        "modelname": "minimax/minimax-m2.5",
        "model_params": {
            "extra_body": {
                "provider": {"only": ["minimax/fp8"]}
            }
        }
    },
    "kimi-k2.5":{
        "base_url": "https://api.moonshot.cn/v1",
        "api_key": "<YOUR_API_KEY>",
        "modelname": "kimi-k2.5"
    }
}
"""

print("【Available models】:")
for model in api_models:
    print(model)

print("【Selected model】:")
print(args.model)
client = openai.OpenAI(
    base_url=api_models[args.model]['base_url'],
    api_key=api_models[args.model]['api_key'],
)

response = client.chat.completions.create(
    model=api_models[args.model]['modelname'],
    messages=[
        {
            "role": "user",
            "content": "Hello, how are you?"
        }
    ]
)

print("【Raw Response】:")
print(response)
print("【Response Content】:")
print(response.choices[0].message.content)