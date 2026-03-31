/*
 * OctoPrint Dremel 3D45 Plugin JavaScript
 *
 * Custom ViewModel for Dremel 3D45 settings panel.
 */

$(function() {
    function Dremel3D45ViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // ---- Plugin message handler (local-print redirect notifications) ----
        self.onDataUpdaterPluginMessage = function(plugin, data) {
            if (plugin !== "dremel3d45") return;
            if (!data || data.type !== "local_print_redirect") return;

            var safeFilename = _.escape(data.filename || "file");
            var title = "Dremel 3D45";

            switch (data.status) {
                case "uploading":
                    new PNotify({
                        title: title,
                        text: "Uploading <strong>" + safeFilename + "</strong> to Dremel printer&hellip;",
                        type: "info",
                        hide: false
                    });
                    break;
                case "success":
                    new PNotify({
                        title: title,
                        text: "Print started on Dremel: <strong>" + safeFilename + "</strong>",
                        type: "success",
                        hide: true,
                        delay: 8000
                    });
                    break;
                case "error":
                    new PNotify({
                        title: title,
                        text: "Failed to redirect print: " + _.escape(data.message || "Unknown error"),
                        type: "error",
                        hide: false
                    });
                    break;
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: Dremel3D45ViewModel,
        dependencies: ["settingsViewModel"]
    });

    // Test connection button handler (outside Knockout bindings)
    $(document).on("click", "#settings-dremel3d45-test_connection", function() {
        var btn = $(this);
        var result = $("#settings-dremel3d45-test_result");
        btn.prop("disabled", true).html("Testing\u2026");
        result.hide();

        OctoPrint.simpleApiCommand("dremel3d45", "test_connection", {})
            .done(function(resp) {
                if (resp.ok) {
                    result.text("\u2713 Connected: " + resp.model +
                                " (FW " + resp.firmware + ", SN " + resp.serial + ")")
                          .removeClass("text-error").addClass("text-success").show();
                } else {
                    result.text("\u2717 " + (resp.error || "Connection failed"))
                          .removeClass("text-success").addClass("text-error").show();
                }
            })
            .fail(function() {
                result.text("\u2717 Request failed")
                      .removeClass("text-success").addClass("text-error").show();
            })
            .always(function() {
                btn.prop("disabled", false)
                   .html('<i class="fas fa-plug"></i> Test Connection');
            });
    });
});
