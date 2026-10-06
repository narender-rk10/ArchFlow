"""The recommended FoodRush option and requirements produced by the live LLM in a real end-to-end evaluation.

Its gaps (found by the AI review, missed by the old deterministic checks): the WebSocket gateway has no path to
auth, courier locations go into Kafka but nothing flows back to the WebSocket gateway, there is no key management
despite an encryption NFR, and notifications, maps, payouts and observability are missing.
"""

IDEA = ("FoodRush: a food-delivery marketplace. Customers order from restaurants and track couriers live on a map, "
        "restaurants manage menus, couriers get assigned automatically. 50k orders/day, EU/GDPR.")


def _c(key, name, type_, tech, layer):
    return {"key": key, "name": name, "type": type_, "technology": tech, "description": "", "layer": layer}


def flawed_option() -> dict:
    return {
        "id": "arch-1", "name": "Event-Driven Microservices with MSK and Custom WebSockets",
        "summary": "Containerized microservices with Kafka and a WebSocket service.", "pros": ["Scalable"], "cons": ["Complex"],
        "best_for": "Large teams", "cost": "high", "complexity": "high", "recommended": True,
        "components": [
            _c("customer-app", "Customer Mobile/Web App", "client", "React Native / React", 0),
            _c("courier-app", "Courier App", "client", "React Native", 0),
            _c("restaurant-app", "Restaurant Portal", "client", "React", 0),
            _c("api-gateway", "API Gateway", "gateway", "AWS API Gateway / Kong", 1),
            _c("auth-service", "Identity & Auth Service", "security", "Amazon Cognito / Keycloak", 1),
            _c("ws-gateway", "WebSocket / MQTT Gateway", "gateway", "Node.js Socket.io on EKS", 1),
            _c("restaurant-service", "Restaurant & Search Service", "service", "Go / Node.js on EKS", 2),
            _c("order-service", "Order Management Service", "service", "Java / Spring Boot on EKS", 2),
            _c("delivery-service", "Delivery & Assignment Service", "service", "Go on EKS", 2),
            _c("matching-worker", "Event-Driven Matching Worker", "service", "Go on EKS", 2),
            _c("kafka", "Event Bus", "queue", "Amazon MSK (Kafka)", 3),
            _c("redis", "Session & State Cache", "cache", "Amazon ElastiCache (Redis)", 3),
            _c("postgis-db", "Restaurant & Menu Database", "database", "Amazon RDS PostgreSQL + PostGIS", 4),
            _c("order-db", "Orders & Delivery Database", "database", "Amazon Aurora PostgreSQL", 4),
            _c("stripe", "Payment Gateway", "external", "Stripe API", 3),
        ],
        "connections": [{"from": a, "to": b, "protocol": p} for a, b, p in [
            ("customer-app", "api-gateway", "HTTPS"), ("restaurant-app", "api-gateway", "HTTPS"),
            ("courier-app", "api-gateway", "HTTPS"), ("customer-app", "ws-gateway", "WSS"),
            ("courier-app", "ws-gateway", "WSS"), ("api-gateway", "auth-service", "HTTPS"),
            ("api-gateway", "restaurant-service", "HTTP/gRPC"), ("api-gateway", "order-service", "HTTP/gRPC"),
            ("api-gateway", "delivery-service", "HTTP/gRPC"), ("restaurant-service", "postgis-db", "TCP"),
            ("order-service", "order-db", "TCP"), ("order-service", "stripe", "HTTPS"), ("order-service", "kafka", "TCP"),
            ("delivery-service", "kafka", "TCP"), ("kafka", "matching-worker", "TCP"),
            ("matching-worker", "delivery-service", "gRPC"), ("delivery-service", "order-db", "TCP"),
            ("ws-gateway", "redis", "TCP"), ("ws-gateway", "kafka", "TCP")]],
    }


def requirements() -> list[dict]:
    return [
        {"title": "Geospatial Restaurant Search", "kind": "functional", "actors": ["Customer"],
         "description": "Allow users to find nearby food options efficiently using geospatial queries.",
         "acceptance_criteria": ["Customers can search for restaurants within a specific radius",
                                 "Results return within 300ms using PostGIS indexing"]},
        {"title": "Order and Payment Processing", "kind": "functional", "actors": ["Customer", "Restaurant"],
         "description": "End-to-end checkout flow including external payment processing and order state management.",
         "acceptance_criteria": ["Customers can assemble an order and checkout", "Payment is securely processed via Stripe"]},
        {"title": "Event-Driven Courier Assignment", "kind": "functional", "actors": ["Courier", "System"],
         "description": "Asynchronous process to find the optimal courier for a prepared order.",
         "acceptance_criteria": ["Orders ready for delivery trigger a match event"]},
        {"title": "High-Frequency Live Tracking", "kind": "functional", "actors": ["Courier", "Customer"],
         "description": "Live bidirectional tracking utilizing a real-time protocol.",
         "acceptance_criteria": ["Couriers transmit location every 1-3 seconds", "Customers see courier movement live on a map"]},
        {"title": "EU GDPR Compliance", "kind": "non_functional", "category": "Compliance", "actors": ["System"],
         "description": "Strict adherence to data privacy laws affecting European operations.",
         "acceptance_criteria": ["All PII is encrypted at rest and in transit", "Servers and databases are hosted in an EU region",
                                 "Right to be forgotten is implemented for customer/courier data"]},
        {"title": "Peak Load Scalability", "kind": "non_functional", "category": "Performance", "actors": ["System"],
         "description": "Infrastructure must scale elastically to absorb high daily burst traffic.",
         "acceptance_criteria": ["System handles 50,000 orders per day smoothly", "APIs maintain a 99.9% uptime SLA"]},
    ]
