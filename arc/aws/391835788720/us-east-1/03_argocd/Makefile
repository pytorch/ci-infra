# Include shared logic
include ../../../../makefiles/layer-common.mk

# Override remote-state.tf to depend on infra layer
.PHONY: remote-state.tf
remote-state.tf:
	@echo "Generating remote state data sources for $(LAYER)..."
	@> remote-state.tf  # Clear file first
	@$(call generate-remote-state,helm,runners)
