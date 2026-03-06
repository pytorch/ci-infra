SHELL=/bin/bash -o pipefail
PROHOME := $(realpath ../../..)

REGION := $(notdir $(CURDIR))
ACCOUNT := $(notdir $(patsubst %/,%,$(dir $(CURDIR))))

ifneq ($(GITHUB_ACTIONS),true)
export AWS_PROFILE := $(ACCOUNT)
endif

# Default target
.PHONY: all
all:
	@echo "$(ACCOUNT) Terraform orchestrator"
	@echo ""
	@echo "Usage:"
	@echo "  make LAYER=01_infra init-layer"
	@echo "  make LAYER=02_helm plan-layer"
	@echo "  make LAYER=03_argocd apply-layer"
	@echo "  make apply"
	@echo ""
	@echo "Available layers: 01_infra, 02_helm, 03_argocd"
	@echo "Available layer commands: init-layer, plan-layer, apply-layer, destroy-layer, clean-layer, tflint-layer"
	@echo "Available all-commands: init, plan, apply, destroy, clean, tflint"
	@exit 1

LAYERS := 01_infra 02_helm 03_argocd
VALID_LAYERS := $(LAYERS)

# Layer validation for single-layer commands
ifdef LAYER
ifeq (,$(filter $(LAYER),$(VALID_LAYERS)))
$(error Invalid LAYER: $(LAYER). Must be one of: $(VALID_LAYERS))
endif
ifeq (,$(wildcard $(LAYER)))
$(error Layer path $(LAYER) does not exist)
endif
endif

# Export variables for child makefiles
export PROHOME ACCOUNT REGION

# Single layer commands - delegate to layer-specific makefile
.PHONY: init-layer plan-layer apply-layer destroy-layer clean-layer tflint-layer
init-layer plan-layer apply-layer destroy-layer clean-layer tflint-layer:
ifndef LAYER
	$(error LAYER is required for single commands. Usage: make REGION=$(REGION) LAYER=01_infra $@)
endif
	@echo "=== Running $@ for $(LAYER) layer in $(ACCOUNT)/$(REGION) ==="
	cd $(LAYER) && $(MAKE) $(word 1,$(subst -, ,$@))

# Multi-layer orchestration commands
.PHONY: init apply destroy clean tflint plan

# Initialize all layers
init:
	@echo "=== Initializing all layers for $(ACCOUNT)/$(REGION) ==="
	$(foreach layer,$(LAYERS),$(MAKE) REGION=$(REGION) LAYER=$(layer) init-layer &&) true

apply:
	@echo "=== Deploying all layers for $(ACCOUNT)/$(REGION) ==="
	$(foreach layer,$(LAYERS),$(MAKE) REGION=$(REGION) LAYER=$(layer) apply-layer &&) true

plan:
	@echo "=== Planning all layers for $(ACCOUNT)/$(REGION) ==="
	$(foreach layer,$(LAYERS),$(MAKE) REGION=$(REGION) LAYER=$(layer) plan-layer &&) true

tflint:
	@echo "=== Linting all layers for $(ACCOUNT)/$(REGION) ==="
	$(foreach layer,$(LAYERS),$(MAKE) REGION=$(REGION) LAYER=$(layer) tflint-layer &&) true

destroy:
	@echo "=== Destroying all layers for $(ACCOUNT)/$(REGION) (reverse order) ==="
	$(foreach layer,$(call reverse,$(LAYERS)),$(MAKE) REGION=$(REGION) LAYER=$(layer) destroy-layer &&) true

clean:
	@echo "=== Cleaning all layers for $(ACCOUNT)/$(REGION) ==="
	$(foreach layer,$(LAYERS),$(MAKE) REGION=$(REGION) LAYER=$(layer) clean-layer &&) true

# Utility function to reverse list (for destroy order)
reverse = $(if $(1),$(call reverse,$(wordlist 2,$(words $(1)),$(1)))) $(firstword $(1))

# Region status check
.PHONY: status
status:
	@echo "=== Status: $(ACCOUNT)/$(REGION) ==="
	@for layer in $(LAYERS); do \
		echo "--- $$layer layer ---"; \
		if [ -d "$(REGION)/$$layer" ]; then \
			cd "$(REGION)/$$layer" && $(MAKE) --quiet status 2>/dev/null || echo "  Status: Not initialized"; \
		else \
			echo "  Status: Directory missing"; \
		fi; \
		echo ""; \
	done

# Help target
.PHONY: help
help: all
