import re
from datetime import date

from django import forms

from .models import Violation


class ViolationForm(forms.ModelForm):
    send_now = forms.BooleanField(required=False, initial=True,
        label="Issue immediately: email the owner and send to the print/mail provider")

    class Meta:
        model = Violation
        fields = ["owner", "vtype", "observed_on", "description", "cure_required", "cure_days", "safety_issue"]
        widgets = {"observed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "description": forms.Textarea(attrs={"rows": 3}),
                   "cure_required": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        data = super().clean()
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
    decision = forms.ChoiceField(choices=[("proceed", "Propose fine/suspension — send to committee hearing"),
                                          ("dismiss", "Dismiss"), ("extend", "Grant more time / second notice")])
    meeting_date = forms.DateField(widget=DateInput, initial=date.today, label="Board meeting date")
    proposed_fine = forms.DecimalField(required=False, min_value=0, decimal_places=2, max_digits=8,
                                       label="Proposed fine ($; up to $100/day, $1,000 total)")
    proposed_suspension = forms.CharField(required=False, label="Suspension: which Common Areas / facilities (if any)")
    proposed_suspension_days = forms.IntegerField(required=False, min_value=1, max_value=730, label="Suspension: number of days")
    board_votes_for = forms.IntegerField(required=False, min_value=0, label="Board votes for")
    board_votes_against = forms.IntegerField(required=False, min_value=0, label="Board votes against")
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


class CommitteeForm(forms.Form):
    votes_for = forms.IntegerField(min_value=0, label="Votes to approve fine")
    votes_against = forms.IntegerField(min_value=0, label="Votes against")
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


class HouseholdForm(forms.Form):
    name = forms.CharField(max_length=150, label="Name")
    email = forms.EmailField(label="Email (they sign in with this)")
    relationship = forms.ChoiceField(choices=[("co_owner", "Co-owner / spouse"), ("tenant", "Tenant / renter"),
                                              ("other", "Other household member")])


class ImportForm(forms.Form):
    file = forms.FileField(label="Owner list (Excel .xlsx or CSV)")
    invite = forms.BooleanField(required=False, label="Also email new owners an invitation to create their login")
