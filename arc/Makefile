SHELL=/bin/bash -o pipefail

.PHONY: clean
clean:
	$(RM) -r .terraform
	$(RM) -r tf-modules
	$(RM) -r venv

.PHONY: tflint
tflint:
	cd modules ; for module in ./*/ ; do \
		pushd $$module ; \
		echo "==== START TFLINT: modules/$$module ============================================" ; \
		tofu init && tflint --init && tflint --module --color --minimum-failure-severity=warning --recursive ; \
		echo "==== END TFLINT: modules/$$module ============================================" ; \
		popd ; \
	done
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START TFLINT: aws/$$account/$$region ============================================" ; \
			$(MAKE) tflint || exit 1 ; \
			echo "==== END TFLINT: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: plan
plan:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make plan: aws/$$account/$$region ============================================" ; \
			$(MAKE) plan || exit 1 ; \
			echo "==== END make plan: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done

.PHONY: apply
apply:
	cd aws ; for account in ./*/ ; do \
		pushd $$account ; \
		for region in ./*/ ; do \
			pushd $$region ; \
			echo "==== START make apply: aws/$$account/$$region ============================================" ; \
			$(MAKE) apply || exit 1 ; \
			echo "==== END make apply: aws/$$account/$$region ============================================" ; \
			popd ; \
		done ; \
		popd ; \
	done
