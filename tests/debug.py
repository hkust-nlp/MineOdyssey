from openai import OpenAI

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key="<YOUR_API_KEY>",
)

response = client.chat.completions.create(
    model="google/gemini-3-flash-preview",
    messages=[
        {
                "role": "user", 
                "content": [
                {
                    "type": "text",
                    "text": "what is 1+1?"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dd/Gfp-wisconsin-madison-the-nature-boardwalk.jpg/2560px-Gfp-wisconsin-madison-the-nature-boardwalk.jpg"
                    }
                },
                {
                    "type": "text",
                    "text": "And also tell me what is 2+2?"
                },
                {
                    "type": "text",
                    "text": "Also tell me the content of this image!!"
                }
            ]
            }
    ],
    extra_body={
        "reasoning": {
            "effort": "low"
        }
    },
)

from pprint import pprint

pprint(response)

import pdb
pdb.set_trace()

msg = response.choices[0].message
print(getattr(msg, "reasoning", None))
