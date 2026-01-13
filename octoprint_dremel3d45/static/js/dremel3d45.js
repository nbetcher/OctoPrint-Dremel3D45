/*
 * OctoPrint Dremel 3D45 Plugin JavaScript
 *
 * Custom ViewModel for Dremel 3D45 settings panel.
 */

$(function() {
    function Dremel3D45ViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        // Custom observables for SD index display
        self.sdIndexConnected = ko.observable(false);
        self.sdIndexCount = ko.observable(0);
        self.sdIndexItems = ko.observableArray([]);

        self.refreshSdIndex = function() {
            return OctoPrint.simpleApiGet("dremel3d45")
                .done(function(resp) {
                    var sd = (resp && resp.sd_index) ? resp.sd_index : { count: 0, items: [] };
                    self.sdIndexConnected(!!resp.connected);
                    self.sdIndexCount(sd.count || 0);
                    self.sdIndexItems(sd.items || []);
                })
                .fail(function() {
                    self.sdIndexConnected(false);
                    self.sdIndexCount(0);
                    self.sdIndexItems([]);
                });
        };

        self.clearSdIndex = function() {
            if (!confirm("Clear the Dremel SD index mapping? This does not delete files from the printer, it only forgets the upload name mapping.")) {
                return;
            }

            OctoPrint.simpleApiCommand("dremel3d45", "clear_sd_index", {})
                .done(function() {
                    self.refreshSdIndex();
                });
        };

        self.onSettingsShown = function() {
            self.refreshSdIndex();
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: Dremel3D45ViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#settings_plugin_dremel3d45"]
    });
});
