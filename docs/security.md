# Security and privacy notes

- Configuration examples are generic and contain **no** credentials or private topology.
- `config.local.json` is ignored by Git. Do not copy credentials into it; this monitor needs none.
- `runtime/` is ignored by Git. Its state and reports may expose local service names and should not be committed.
- TCP checks are loopback only. The application makes no outbound internet requests, does not listen on any port, and has no dashboard exposed to the network.
- The Python monitor does not use `sudo`, modify the firewall, restart services, or perform backup operations. Optional systemd scheduling templates are included but are not installed or enabled by this package.
- Local files are created using a restrictive process umask. Protect the repository directory against access by other users.
- This is a learning/lab prototype, not a security monitoring or intrusion detection system.
