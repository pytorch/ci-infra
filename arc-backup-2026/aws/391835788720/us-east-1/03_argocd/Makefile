# Include shared logic
include ../../../../makefiles/layer-common.mk

# Override remote-state.tf to depend on infra layer
.PHONY: remote-state.tf
remote-state.tf:
	@echo "Generating remote state data sources for $(LAYER)..."
	sed -e "s/#AWS_REGION/$(REGION)/g" -e "s/#BACKEND_KEY/helm/g" <$(PROHOME)/templates/backend-file/remote-state.tf >remote-state.tf
