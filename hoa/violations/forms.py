import re
from datetime import date

from django import forms

from .models import Violation


class ViolationForm(forms.ModelForm):
    timing = forms.ChoiceField(
        label="Violation Issuance Timing", initial="standard", widget=forms.RadioSelect,
        choices=[("standard", "Standard: email the owner now, then hold the mailed copy 2 business days "
                              "(if it is corrected in that time, the postage is saved)"),
                 ("urgent", "Issue immediately - urgent: email and send to US Mail right now"),
                 ("draft", "Save as a draft (don't send yet)")])

    class Meta:
        model = Violation
        fields = ["owner", "vtype", "observed_on", "description", "cure_required", "cure_days", "non_curable", "safety_issue", "board_emergency"]
        labels = {"non_curable": "Non curable violation (dog off leash, parking in street, blocked sidewalk, noise violation)"}
        widgets = {"observed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "description": forms.Textarea(attrs={"rows": 3}),
                   "cure_required": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        data = super().clean()
        o = data.get("owner")
        if o:   # we already know the address: fill "[location]" / "[address]" instead of asking the user to type it
            for f in ("description", "cure_required"):
                data[f] = re.sub(r"\[(location|address|property address)\]", lambda m: o.property_address, data.get(f) or "", flags=re.I)
        if data.get("non_curable"):
            data["cure_days"] = 0   # nothing to cure, so no cure period
        for f, label in (("description", "What was observed"), ("cure_required", "Required cure")):
            if re.search(r"\[[^\]]*\]", data.get(f) or ""):
                self.add_error(f, f"{label} still has [bracketed] text. Replace it with the specifics before sending.")
        return data

    def clean_observed_on(self):
        d = self.cleaned_data["observed_on"]
        if d > date.today():
            raise forms.ValidationError("Observation date can't be in the future.")
        return d


class DateInput(forms.DateInput):
    input_type = "date"


class ExtensionForm(forms.Form):
    days = forms.IntegerField(required=False, min_value=1, max_value=365, label="Add this many days to the deadline")
    new_deadline = forms.DateField(widget=DateInput, required=False, label="...or set an exact new deadline")
    second_notice = forms.BooleanField(required=False, label="Send as a full second Notice of Violation (otherwise a short extension letter)")
    reason = forms.CharField(required=False)

    def clean(self):
        d = super().clean()
        if not d.get("days") and not d.get("new_deadline"):
            raise forms.ValidationError("Enter the days to add, or an exact new deadline.")
        return d


class ExtApproveForm(forms.Form):
    response_id = forms.IntegerField(widget=forms.HiddenInput)
    days = forms.IntegerField(required=False, min_value=1, max_value=365, label="Days to add (edit if different)")
    new_deadline = forms.DateField(widget=DateInput, required=False, label="or exact new deadline (optional)")
    second_notice = forms.BooleanField(required=False, label="Send as a full second Notice of Violation")
    note = forms.CharField(required=False, label="Note for the record (optional)")

    def clean(self):
        d = super().clean()
        if not d.get("days") and not d.get("new_deadline"):
            raise forms.ValidationError("Enter the days to add, or an exact new deadline.")
        return d


class ExtDeclineForm(forms.Form):
    response_id = forms.IntegerField(widget=forms.HiddenInput)
    reason = forms.CharField(required=False, label="Reason (the owner will see this)")


class ReplyForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class MailedForm(forms.Form):
    mailed_on = forms.DateField(widget=DateInput, initial=date.today, label="Mailed / delivered on")
    method = forms.ChoiceField(choices=[("first_class", "First-class mail"), ("certified", "Certified, return receipt"),
                                        ("hand", "Hand delivered")], required=False)
    tracking_number = forms.CharField(required=False)
    return_receipt_on = forms.DateField(widget=DateInput, required=False, label="Return receipt back on")


class PaymentForm(forms.Form):
    paid_on = forms.DateField(widget=DateInput, initial=date.today)


class NoteForm(forms.Form):
    note = forms.CharField(required=False)


class BoardForm(forms.Form):
    decision = forms.ChoiceField(choices=[("proceed", "Fine issued: send to the Compliance Committee"),
                                          ("dismiss", "Dismiss"), ("extend", "Grant more time / second notice")])
    meeting_date = forms.DateField(widget=DateInput, initial=date.today, label="Board meeting date")
    proposed_fine = forms.DecimalField(required=False, min_value=0, decimal_places=2, max_digits=8,
                                       label="Fine ($; up to $100/day, $1,000 total)")
    by_majority = forms.BooleanField(required=False, label="The Board approved this by majority vote at its meeting")
    extension_days = forms.IntegerField(required=False, min_value=1, max_value=365, label="Days to add (if granting time)")
    new_deadline = forms.DateField(widget=DateInput, required=False, label="or exact new deadline (if granting time)")
    note = forms.CharField(required=False)


class MeetingForm(forms.Form):
    starts_at = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                                    input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"], label="Meeting date and time")
    zoom_link = forms.URLField(required=False, label="Zoom link", assume_scheme="https")
    zoom_meeting_id = forms.CharField(required=False, label="Zoom meeting ID")
    zoom_passcode = forms.CharField(required=False, label="Zoom passcode")
    zoom_dial_in = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Zoom one-tap / dial-in lines (paste from the invitation)")
    location = forms.CharField(required=False, label="In-person location (optional)")


class AssignForm(forms.Form):
    meeting = forms.ModelChoiceField(queryset=None, label="Committee meeting")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from django.utils import timezone
        from .models import CommitteeMeeting
        self.fields["meeting"].queryset = CommitteeMeeting.objects.filter(starts_at__gt=timezone.now()).order_by("starts_at")


class HoldForm(forms.Form):
    reason = forms.CharField(label="Reason for hold")
    until = forms.DateField(label="On hold until", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                            help_text="The hold lifts itself on this date and the cure period starts over.")

    def clean_until(self):
        d = self.cleaned_data["until"]
        if d <= date.today():
            raise forms.ValidationError("Pick a date after today.")
        return d


class CommitteeForm(forms.Form):
    votes_for = forms.IntegerField(min_value=0, label="Votes to approve fine")
    votes_against = forms.IntegerField(min_value=0, label="Votes against")
    findings = forms.CharField(label="Findings of fact (quoted in the decision letter)", widget=forms.Textarea(attrs={"rows": 3}))
    cure_by = forms.DateField(required=False, label="No fine if cured by (optional)", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    note = forms.CharField(required=False)


class ResidentForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=5000)
    cure_reported = forms.BooleanField(required=False, label="The violation has been fixed (please reinspect)")
    request_extension = forms.BooleanField(required=False, label="I need more time to cure")
    attorney_notice = forms.BooleanField(required=False, label="I will bring an attorney to the hearing")
    extension_days = forms.IntegerField(required=False, min_value=1, max_value=180, label="How many more days?")

    def clean(self):
        d = super().clean()
        if d.get("request_extension") and not d.get("extension_days"):
            self.add_error("extension_days", "Tell us how many more days you need.")
        return d


class ImposeForm(forms.Form):
    method = forms.ChoiceField(choices=[("api", "Post through the Vantaca API"), ("manual", "I posted it in Vantaca by hand")], initial="manual")
    reference = forms.CharField(required=False, label="Vantaca transaction / reference # (optional)")


class ImportForm(forms.Form):
    file = forms.FileField(label="Owner list (Excel .xlsx or CSV)")

