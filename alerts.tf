# Email alert for heavy data transfer. The aws provider has no Lightsail alarm
# resource, so it comes from awscc (AWS Cloud Control). Set alert_email to enable it.
#
# The email contact method is a one-time CLI step: Cloud Control has no
# AWS::Lightsail::ContactMethod type in ap-southeast-5 (TypeNotFoundException).
#   aws lightsail create-contact-method --region <region> --protocol Email --contact-endpoint <email>
# AWS then emails a verification link; the alarm only notifies after it is clicked.

# Lightsail alarms look at 5-minute datapoints, at most 288 of them (24 h), so this
# watches pace rather than the monthly total (the monitor app shows the total).
resource "awscc_lightsail_alarm" "transfer_pace" {
  count                   = var.alert_email == "" ? 0 : 1
  alarm_name              = "${var.name}-transfer-pace"
  monitored_resource_name = aws_lightsail_instance.wg.name
  metric_name             = "NetworkOut"
  comparison_operator     = "GreaterThanThreshold"
  threshold               = var.alarm_out_bytes_per_5min
  evaluation_periods      = 288 # the last 24 hours
  datapoints_to_alarm     = 72  # above pace for 6 of those hours
  treat_missing_data      = "notBreaching"
  contact_protocols       = ["Email"]
  notification_triggers   = ["ALARM"]
  notification_enabled    = true
}
