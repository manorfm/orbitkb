"""Structural matching for declared HTTP routes and security patterns."""


def route_pattern_covers(security_pattern: str, api_path: str) -> bool:
    """Match single segments and a trailing ``**`` against a declared route."""
    security_segments = [segment for segment in security_pattern.split("/") if segment]
    api_segments = [segment for segment in api_path.split("/") if segment]
    for index, security_segment in enumerate(security_segments):
        if security_segment == "**":
            return True
        if index >= len(api_segments):
            return False
        api_segment = api_segments[index]
        if not (security_segment == "*" or security_segment.startswith("{")
                or api_segment.startswith("{") or security_segment == api_segment):
            return False
    return len(api_segments) == len(security_segments)
