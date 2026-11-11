from klkg.extraction.llm_client import LlmClient


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.payloads = []
        self.headers = {}

    def post(self, url, json, timeout):
        self.payloads.append(json)
        choices = [{"message": {"content": "{}"}, "finish_reason": "stop"} for _ in range(json["n"])]
        return FakeResponse({"choices": choices, "usage": {"prompt_tokens": 100, "completion_tokens": 20 * json["n"]}})


def client(supports_n):
    instance = LlmClient("http://llm/v1", "model", 0.7, 0.95, 256, 5, supports_n, 7, 10)
    instance.session = FakeSession()
    return instance


def test_single_request_with_n():
    instance = client(True)
    completion = instance.complete([{"role": "user", "content": "x"}], {"type": "object"}, seed_offset=3)
    assert len(instance.session.payloads) == 1
    assert instance.session.payloads[0]["n"] == 5
    assert instance.session.payloads[0]["seed"] == 10
    assert instance.session.payloads[0]["response_format"]["type"] == "json_schema"
    assert len(completion.texts) == 5
    assert completion.completion_tokens == 100


def test_one_request_per_sample_without_n():
    instance = client(False)
    completion = instance.complete([{"role": "user", "content": "x"}], {"type": "object"})
    assert len(instance.session.payloads) == 5
    assert len({payload["seed"] for payload in instance.session.payloads}) == 5
    assert len(completion.texts) == 5
    assert completion.prompt_tokens == 500
