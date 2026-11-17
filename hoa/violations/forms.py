from datetime import date

from django import forms

from .models import Violation


class ViolationForm(forms.ModelForm):
    class Meta:
        model = Violation
        fields = ["owner", "vtype", "observed_on", "description", "cure_required", "cure_days"]
        widgets = {"observed_on": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "description": forms.Textarea(attrs={"rows": 3}),
                   "cure_required": forms.Textarea(attrs={"rows": 3})}

    def clean_observed_on(self):
        d = self.cleaned_data["observed_on"]
        if d > date.today():
            raise forms.ValidationError("Observation date can't be in the future.")
        return d


class DateInput(forms.DateInput):
    input_type = "date"


class ExtensionForm(forms.Form):
    new_deadline = forms.DateField(widget=DateInput)
    second_notice = forms.BooleanField(required=False, label="Send as a full second Notice of Violation (otherwise a short extension letter)")
    reason = forms.CharField(required=False)


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
    proposed_suspension = forms.CharField(required=False, label="Proposed suspension (if any)")
    new_deadline = forms.DateField(widget=DateInput, required=False, label="New cure deadline (if granting time)")
    note = forms.CharField(required=False)


class HearingForm(forms.Form):
    hearing_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                                       input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"])
    location = forms.CharField(required=False, label="Place / virtual meeting link")


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
