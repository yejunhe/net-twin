import json


def get_json_content(json_path):
    with open(json_path, 'r', encoding = 'utf-8') as f:
        data = json.load(f)

    return data

