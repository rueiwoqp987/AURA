from .s1_engine import S1Engine
from .s2_engine import S2Engine
from .s3_engine import S3Engine

ENGINE_REGISTRY = {
    "S1": S1Engine(),
    "S2": S2Engine(),
    "S3": S3Engine(),
}
