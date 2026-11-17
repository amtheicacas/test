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
    reason = forms.CharField(required=False)


class ReplyForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}))


class MailedForm(forms.Form):
    mailed_on = forms.DateField(widget=DateInput, initial=date.today)
    tracking_number = forms.CharField(required=False)


class BoardForm(forms.Form):
    decision = forms.ChoiceField(choices=[("proceed", "Proceed — schedule fining hearing"),
                                          ("dismiss", "Dismiss"), ("extend", "Grant more time")])
    meeting_date = forms.DateField(widget=DateInput, initial=date.today)
    new_deadline = forms.DateField(widget=DateInput, required=False, label="New cure deadline (if granting time)")
    note = forms.CharField(required=False)


class HearingForm(forms.Form):
    hearing_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                                       input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"])
    location = forms.CharField(required=False)


class CommitteeForm(forms.Form):
    votes_for = forms.IntegerField(min_value=0, label="Votes to approve fine")
    votes_against = forms.IntegerField(min_value=0, label="Votes against")
    fine_amount = forms.DecimalField(min_value=0, decimal_places=2, max_digits=8, required=False)
    note = forms.CharField(required=False)


class ResidentForm(forms.Form):
    message = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), max_length=5000)
    request_extension = forms.BooleanField(required=False, label="I need more time to cure")
    extension_days = forms.IntegerField(required=False, min_value=1, max_value=180, label="How many more days?")
