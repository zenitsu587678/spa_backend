from django.conf import settings


class SimpleCorsMiddleware:
    """Tiny CORS layer so the public site can POST to this local server.

    Replace with django-cors-headers if you need finer control.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            from django.http import HttpResponse
            response = HttpResponse(status=204)
        else:
            response = self.get_response(request)

        allowed = settings.CORS_ALLOWED_ORIGINS
        origin = request.headers.get("Origin", "")
        if "*" in allowed:
            response["Access-Control-Allow-Origin"] = "*"
        elif origin in allowed:
            response["Access-Control-Allow-Origin"] = origin
            response["Vary"] = "Origin"
        response["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Key"
        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Max-Age"] = "86400"
        return response
