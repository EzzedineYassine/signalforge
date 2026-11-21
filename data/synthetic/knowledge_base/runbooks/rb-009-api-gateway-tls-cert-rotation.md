---
doc_id: rb-009
doc_type: runbook
title: API Gateway TLS Certificate Rotation and Handshake Recovery
service: api-gateway
tags: [api-gateway, tls, certificates, handshake, security]
is_synthetic: true
---

> **[SYNTHETIC DOCUMENT — FOR EVALUATION ONLY]**

# API Gateway TLS Certificate Rotation and Handshake Recovery

## Symptoms

Clients report TLS handshake failures (`ERR_CERT_DATE_INVALID` or `x509: certificate has expired`) while `api-gateway` logs elevated `tls_handshake_error` events and sharp drops in authenticated `request_throughput`.

## Verification Commands

```bash
openssl s_client -connect api-gateway.internal:443 -servername api.signalforge.local </dev/null 2>/dev/null   | openssl x509 -noout -dates -subject -issuer
```

## Remediation

1. Trigger emergency certificate renewal via the internal PKI / cert-manager controller and verify the new leaf + intermediate chain bundle.
2. Reload `api-gateway` TLS listeners gracefully (`SIGHUP`) without dropping active in-flight checkout connections.
3. Confirm new `notAfter` timestamp > 60 days out and verify `tls_handshake_error` count drops to zero across all gateway replicas.
