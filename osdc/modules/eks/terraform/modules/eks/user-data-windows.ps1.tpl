<powershell>
[string]$EKSBootstrapScriptFile = "$env:ProgramFiles\Amazon\EKS\Start-EKSBootstrap.ps1"
& $EKSBootstrapScriptFile -EKSClusterName ${cluster_name} -Base64ClusterCA ${cluster_ca} -APIServerEndpoint ${cluster_endpoint} -DNSClusterIP ${dns_cluster_ip}
</powershell>
